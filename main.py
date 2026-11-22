"""Оркестратор: vacancies.json -> генерация текста -> пакетная отправка -> sent_log.csv

Запуск: python main.py
"""

import csv
import json
import logging
import os
import sys
from datetime import datetime

import config
import llm_writer
import mailer


def load_vacancies(path):
    """vacancies.json — список объектов с полями title, company, description, hr_email."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("vacancies", [])
    if not isinstance(data, list):
        raise ValueError("vacancies.json должен содержать список вакансий")
    return data


def load_already_sent(path):
    """Адреса со статусом sent из sent_log.csv — повторная отправка запрещена."""
    already = set()
    if not path.exists():
        return already
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip().lower() == "sent":
                address = (row.get("to_email") or "").strip().lower()
                if address:
                    already.add(address)
    return already


def append_log(rows):
    """Дописывает строки в sent_log.csv. Файл создаётся с BOM (utf-8-sig),
    чтобы корректно открываться в Excel; дозапись — обычный utf-8."""
    if not rows:
        return
    new_file = not config.SENT_LOG_CSV.exists()
    encoding = "utf-8-sig" if new_file else "utf-8"
    with open(config.SENT_LOG_CSV, "a", encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=config.SENT_LOG_FIELDS)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
        f.flush()
        os.fsync(f.fileno())  # строка журнала переживает падение процесса


def log_row(company, vacancy, to_email, status, note=""):
    return {
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "company": company,
        "vacancy": vacancy,
        "to_email": to_email,
        "status": status,
        "note": note,
    }


# символы, недопустимые в именах файлов Windows
_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def _safe_filename_part(text):
    """company -> безопасная часть имени файла: недопустимые символы -> '_'."""
    part = "".join(
        "_" if (ch in _INVALID_FILENAME_CHARS or ord(ch) < 32) else ch
        for ch in (text or "").strip()
    )
    part = part.strip().strip(".").strip() or "без_компании"
    return part[:80]


def _save_draft(index, company, to_email, subject, body):
    """Сохраняет сгенерированное письмо в drafts/ ДО отправки — независимо
    от результата отправки (текст всегда можно проверить глазами)."""
    config.DRAFTS_DIR.mkdir(exist_ok=True)
    filename = "{:02d}_{}.txt".format(index, _safe_filename_part(company))
    path = config.DRAFTS_DIR / filename
    path.write_text(
        "TO: {}\nSUBJECT: {}\nBODY:\n{}\n".format(to_email, subject, body),
        encoding="utf-8",
    )
    logging.info("Черновик сохранён: %s", filename)
    return path


def run():
    config.setup_logging()
    logging.info("=== Запуск: генерация и отправка откликов ===")

    # --- ранние проверки до начала любой работы (сообщения без значений секретов) ---
    if not os.environ.get("MAILRU_APP_PASSWORD"):
        print("MAILRU_APP_PASSWORD не задан — впиши пароль приложения Mail.ru "
              "в .env (см. README, раздел 3). Запуск отменён.")
        return 1

    if not llm_writer.has_providers():
        print("Ключи LLM не найдены: нет llm_providers.json и переменной DEEPSEEK_API_KEY "
              "(см. README, раздел 4). Запуск отменён.")
        return 1

    profile_path = (config.PROFILE_LETTERS_TXT
                    if config.PROFILE_LETTERS_TXT.exists()
                    else config.PROFILE_TXT)
    if not profile_path.exists():
        print("Не найден файл профиля рядом с main.py "
              "(ожидался: {}).".format(config.PROFILE_LETTERS_TXT.name))
        return 1

    try:
        vacancies = load_vacancies(config.VACANCIES_JSON)
    except (OSError, ValueError) as exc:
        print("Не удалось прочитать vacancies.json: {}".format(exc))
        return 1

    if not vacancies:
        print("vacancies.json пуст — добавь вакансии (формат в README, раздел 5).")
        return 0

    profile_text = profile_path.read_text(encoding="utf-8")
    logging.info("Файл профиля: %s", profile_path.name)
    already_sent = load_already_sent(config.SENT_LOG_CSV)
    if config.ALLOW_RESEND:
        logging.warning("ALLOW_RESEND=True: блок повторных отправок ОТКЛЮЧЁН (тестовый режим!)")
    logging.info("Вакансий на входе: %d; адресов уже в журнале: %d",
                 len(vacancies), len(already_sent))

    messages = []
    skipped_rows = []
    used_in_run = set()
    for vacancy in vacancies:
        if len(messages) >= config.MAX_PER_RUN:
            logging.info("Достигнут лимит пакета (%d писем) — остальное на следующий запуск",
                         config.MAX_PER_RUN)
            break
        title = (vacancy.get("title") or "").strip()
        company = (vacancy.get("company") or "").strip()
        description = (vacancy.get("description") or "").strip()
        hr_email = (vacancy.get("hr_email") or "").strip()
        if not title or not hr_email:
            skipped_rows.append(log_row(company, title, hr_email, "skipped",
                                        "нет названия вакансии или hr_email"))
            continue
        address = hr_email.lower()
        if address in already_sent and not config.ALLOW_RESEND:
            skipped_rows.append(log_row(company, title, hr_email, "skipped",
                                        "на этот адрес уже отправлялось"))
            continue
        if address in used_in_run:
            skipped_rows.append(log_row(company, title, hr_email, "skipped",
                                        "дубль адреса во входном списке"))
            continue
        attach_resume = bool(config.ATTACH_RESUME and config.RESUME_PDF.exists())
        letter = llm_writer.generate_letter(title, company, description, profile_text,
                                            attach_resume=attach_resume)
        if letter is None:
            skipped_rows.append(log_row(company, title, hr_email, "skipped",
                                        "генерация не удалась (подробности в логе)"))
            continue
        subject, body = letter
        used_in_run.add(address)
        messages.append({
            "to": hr_email,
            "subject": subject,
            "body": body,
            "vacancy": title,
            "company": company,
        })
        draft_path = _save_draft(len(messages), company, hr_email, subject, body)
        logging.info("Сгенерировано письмо %d (%s): %s — %s",
                     len(messages), draft_path.name,
                     company or "(без компании)", title)

    append_log(skipped_rows)

    if not messages:
        print("Писем для отправки: 0. Подробности — в logs/app.log и sent_log.csv.")
        return 0

    print("Сгенерировано писем: {}. Отправка началась, паузы 20-60 сек — "
          "не закрывай окно до конца.".format(len(messages)))
    try:
        results = mailer.send_batch(
            messages,
            on_result=lambda r: append_log([
                log_row(r.get("company", ""), r.get("vacancy", ""), r.get("to", ""),
                        r.get("status", "error"), r.get("note", ""))
            ]),
        )
    except mailer.MailAuthError as exc:
        logging.error("Отправка остановлена: %s", exc)
        print("ОСТАНОВКА: {}. Отправленные до ошибки письма уже записаны в sent_log.csv.".format(exc))
        return 2
    except mailer.MailConfigError as exc:
        logging.error("Отправка остановлена: %s", exc)
        print("ОСТАНОВКА: {}. Проверь config.py: From и логин SMTP должны совпадать.".format(exc))
        return 2
    except KeyboardInterrupt:
        print("Остановлено вручную. Отправленные письма уже записаны в sent_log.csv.")
        return 130

    sent = sum(1 for r in results if r["status"] == "sent")
    errors = sum(1 for r in results if r["status"] == "error")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    print("Готово: отправлено {}, ошибок {}, пропущено {}. Журнал: sent_log.csv".format(
        sent, errors, skipped))
    return 0


def _pid_alive(pid_text):
    """Жив ли процесс с таким PID (Windows; для замка от двойного запуска)."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        pid = int(pid_text)
    except (TypeError, ValueError):
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def acquire_bot_lock():
    """True = замок захвачен, можно работать. False = бот уже работает."""
    if config.LOCK_FILE.exists():
        if _pid_alive(config.LOCK_FILE.read_text(encoding="utf-8").strip()):
            return False
        config.LOCK_FILE.unlink(missing_ok=True)  # мёртвый хвост от краша
    config.LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_bot_lock():
    config.LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    if not acquire_bot_lock():
        print("Бот уже работает (живой bot.lock) — второй экземпляр не стартую.")
        sys.exit(3)
    try:
        sys.exit(run())
    finally:
        release_bot_lock()
