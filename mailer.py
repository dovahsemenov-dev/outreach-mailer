"""Единственный модуль, который открывает SMTP и читает почтовый пароль.

Пароль приложения берётся ТОЛЬКО из переменной окружения. Он не попадает
в логи, исключения и возвращаемые значения — только имя переменной окружения.
"""

import logging
import os
import random
import smtplib
import time
from email.message import EmailMessage

import config


class MailAuthError(Exception):
    """Ошибка SMTP-аутентификации — весь запуск должен остановиться."""


class MailConfigError(Exception):
    """Конфигурация отправителя неверна (From не совпадает с логином SMTP) —
    весь запуск должен остановиться."""


def get_app_password():
    """Читает пароль приложения только из окружения. Значение не логируется."""
    password = os.environ.get("MAILRU_APP_PASSWORD")
    if not password:
        raise MailAuthError(
            "MAILRU_APP_PASSWORD не задан — задай переменную окружения (см. README)"
        )
    return password


def _open_connection():
    """Открывает SMTP_SSL-соединение и логинится. Ошибка входа -> MailAuthError."""
    server = smtplib.SMTP_SSL(
        config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT
    )
    try:
        server.login(config.SMTP_LOGIN, get_app_password())
    except smtplib.SMTPException as exc:
        _quit_quietly(server)
        code = getattr(exc, "smtp_code", "")
        logging.error("SMTP-вход не принят (код %s): %s", code, type(exc).__name__)
        raise MailAuthError(
            "SMTP-аутентификация не прошла: проверь пароль приложения Mail.ru "
            "в переменной окружения и логин. Весь запуск остановлен."
        )
    logging.info("SMTP-соединение открыто (%s:%s)", config.SMTP_HOST, config.SMTP_PORT)
    return server


def _quit_quietly(server):
    try:
        server.quit()
    except Exception:
        pass


def _ensure_from_is_login(msg):
    """Инвариант безопасности: From каждого письма обязан совпадать с логином
    SMTP — письмо от чужого адреса Mail.ru отклонит."""
    if msg["From"] != config.SMTP_LOGIN:
        raise MailConfigError(
            "From '{}' не совпадает с логином SMTP из config.py".format(msg["From"])
        )


def _build_message(to, subject, body, attachment=None):
    msg = EmailMessage()
    msg["From"] = config.SMTP_LOGIN
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")  # text/plain, UTF-8
    if attachment is not None:
        msg.add_attachment(
            attachment.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=attachment.name,
        )
    _ensure_from_is_login(msg)
    return msg


def send_batch(messages, on_result=None):
    """Отправляет пакет писем: ОДНО SMTP_SSL-соединение на пакет — открывается
    перед первым отправляемым письмом, закрывается после последнего.

    - перед отправкой: проверка непустых темы/тела и защита от повторного
      адреса в рамках одного запуска;
    - инвариант From = логин SMTP проверяется для каждого письма,
      нарушение поднимает MailConfigError (весь запуск стопается);
    - результаты идут в порядке входных сообщений (письмо или skip);
    - пауза между письмами случайная (config.PAUSE_BETWEEN_MIN..MAX);
    - ошибка одного письма не останавливает пакет (статус error);
    - ошибка аутентификации поднимает MailAuthError (весь запуск стопается).

    on_result — колбэк, вызываемый для каждого результата СРАЗУ (журнал
    переживает падение процесса). Возвращает список результатов:
    {"to", "vacancy", "company", "status": sent|skipped|error, "note"}.
    """
    results = []

    def emit(result):
        results.append(result)
        if on_result:
            try:
                on_result(result)
            except Exception as exc:  # журнал не должен ронять отправку
                logging.warning("Не удалось сразу записать строку журнала: %s",
                                type(exc).__name__)

    def skip(base, note):
        logging.info("Пропуск (%s): %s", base["company"] or "без компании", note)
        emit({**base, "status": "skipped", "note": note})

    attachment = None
    attachment_checked = False

    def attachment_for():
        """Однократно решает, какое вложение прикреплять (если включено)."""
        nonlocal attachment, attachment_checked
        if not attachment_checked:
            attachment_checked = True
            if config.ATTACH_RESUME and config.RESUME_PDF.exists():
                attachment = config.RESUME_PDF
                logging.info("Вложения включены: %s", attachment.name)
            else:
                logging.info("Отправка без вложения resume.pdf")
        return attachment

    seen_addresses = set()
    server = None
    sent_count = 0
    try:
        for msg in messages:
            to = (msg.get("to") or "").strip()
            base = {
                "to": to,
                "vacancy": msg.get("vacancy") or "",
                "company": msg.get("company") or "",
            }
            if not to:
                skip(base, "нет email работодателя")
                continue
            if not (msg.get("subject") or "").strip():
                skip(base, "пустая тема письма")
                continue
            if not (msg.get("body") or "").strip():
                skip(base, "пустое тело письма")
                continue
            if to.lower() in seen_addresses:
                skip(base, "повторный адрес в этом запуске")
                continue
            seen_addresses.add(to.lower())

            if server is None:
                server = _open_connection()
            if sent_count > 0:
                pause = random.uniform(
                    config.PAUSE_BETWEEN_MIN, config.PAUSE_BETWEEN_MAX
                )
                logging.info("Пауза %.0f сек перед следующим письмом", pause)
                time.sleep(pause)
                try:
                    server.noop()
                except (smtplib.SMTPServerDisconnected, OSError):
                    logging.warning("SMTP-соединение разорвано — переподключение")
                    server = _open_connection()

            try:
                letter = _build_message(to, msg["subject"], msg["body"], attachment_for())
                if config.SEND_COPY_TO_SELF:
                    # копия себе через SMTP-конверт: попадает во «Входящие» mail.ru
                    # (SMTP сам не пишет в «Отправленные»), работодатель не видит
                    server.send_message(letter, to_addrs=[to, config.SENDER_EMAIL])
                else:
                    server.send_message(letter)
                sent_count += 1
                emit({**base, "status": "sent", "note": ""})
                logging.info("Отправлено (письмо %d) -> %s", sent_count, to)
            except smtplib.SMTPServerDisconnected:
                emit({**base, "status": "error", "note": "SMTPServerDisconnected"})
                logging.warning("Соединение разорвано при отправке -> %s", to)
                try:
                    server = _open_connection()
                except Exception:
                    logging.error("Не удалось восстановить SMTP-соединение — пакет прерван")
                    break
            except (smtplib.SMTPException, OSError) as exc:
                emit({**base, "status": "error", "note": type(exc).__name__})
                logging.warning("Ошибка отправки -> %s: %s", to, type(exc).__name__)
    finally:
        if server is not None:
            _quit_quietly(server)
            logging.info("SMTP-соединение закрыто")
    return results
