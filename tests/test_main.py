"""Интеграционные проверки оркестратора: ранние проверки секретов и журнал без секретов."""

import csv
import json
import logging
import smtplib
from pathlib import Path

import pytest

import config
import llm_writer
import mailer
import main


class FakeSMTP:
    """Фейковый SMTP_SSL для интеграционных тестов main.py."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.creds = None
        self.sent = []
        self.sent_envelopes = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def login(self, user, password):
        self.creds = (user, password)

    def send_message(self, msg, to_addrs=None):
        if getattr(self, "fail_sends", False):
            raise smtplib.SMTPException("send failed")
        self.sent.append(msg)
        self.sent_envelopes.append(tuple(to_addrs) if to_addrs else None)

    def noop(self):
        pass

    def quit(self):
        self.quit_called = True


def _close_logging_handlers():
    """Не оставлять открытых файловых хендлеров во временные логи."""
    for handler in list(logging.root.handlers):
        handler.close()
        logging.root.removeHandler(handler)


@pytest.fixture(autouse=True)
def isolated_run(monkeypatch, tmp_path):
    """Файлы и SMTP подменены: ни один тест не пишет в реальные данные."""
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(config, "PAUSE_BETWEEN_MIN", 0)
    monkeypatch.setattr(config, "PAUSE_BETWEEN_MAX", 0)
    monkeypatch.setattr(config, "ATTACH_RESUME", False)
    monkeypatch.setattr(config, "SENT_LOG_CSV", tmp_path / "sent_log.csv")
    monkeypatch.setattr(config, "VACANCIES_JSON", tmp_path / "vacancies.json")
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "logs" / "app.log")
    monkeypatch.setattr(config, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.setattr(llm_writer, "_provider_offset", 0)
    yield
    FakeSMTP.instances = []
    _close_logging_handlers()


def _write_vacancies(items):
    config.VACANCIES_JSON.write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )


# --- ТЗ п.1: ранние проверки до начала работы ---

def test_missing_mail_password_stops_before_any_work(monkeypatch):
    monkeypatch.delenv("MAILRU_APP_PASSWORD", raising=False)
    calls = []
    monkeypatch.setattr(llm_writer, "generate_letter", lambda *a, **k: calls.append(1))
    code = main.run()
    assert code == 1
    assert calls == []                       # генерация даже не началась
    assert not config.SENT_LOG_CSV.exists()  # журнал не создан
    assert FakeSMTP.instances == []          # SMTP не открывался


def test_missing_llm_keys_stops_before_any_work(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(config, "LLM_PROVIDERS_JSON", tmp_path / "absent.json")
    calls = []
    monkeypatch.setattr(llm_writer, "generate_letter", lambda *a, **k: calls.append(1))
    code = main.run()
    assert code == 1
    assert calls == []
    assert not config.SENT_LOG_CSV.exists()


# --- ТЗ п.3: в sent_log.csv не попадают пароль и ключ ---

def test_sent_log_and_app_log_contain_no_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret-password")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-deepseek-key")
    _write_vacancies([{
        "title": "Тестировщик (QA)",
        "company": "Ромашка",
        "description": "Ручное тестирование",
        "hr_email": "hr@example.com",
    }])
    _close_logging_handlers()
    config.setup_logging()  # теперь лог пишется во временный файл теста
    # генерация подменена: секреты известны, их должен получить только SMTP-мок
    monkeypatch.setattr(llm_writer, "generate_letter",
                        lambda *a, **k: ("Тема письма", "Тело письма"))

    code = main.run()
    assert code == 0
    assert len(FakeSMTP.instances) == 1
    assert FakeSMTP.instances[0].creds == (config.SMTP_LOGIN, "dummy-secret-password")
    assert len(FakeSMTP.instances[0].sent) == 1

    log_text = Path(config.SENT_LOG_CSV).read_text(encoding="utf-8-sig")
    assert "sent" in log_text  # строка об отправке записана
    assert "dummy-secret-password" not in log_text
    assert "dummy-deepseek-key" not in log_text

    app_log = Path(config.LOG_FILE).read_text(encoding="utf-8")
    assert "dummy-secret-password" not in app_log
    assert "dummy-deepseek-key" not in app_log


# --- ТЗ п.3 (усиление): РЕАЛЬНЫЕ ключи из llm_providers.json не попадают в журнал и лог ---

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


_LONG_BODY = (
    "Здравствуйте! Меня заинтересовала ваша вакансия: у меня есть коммерческий "
    "опыт ручного тестирования, я оформлял тест-кейсы, чек-листы и баг-репорты, "
    "работал с автоматизированными тестами и поддерживал их при изменениях в "
    "продукте. Буду рад обсудить задачу подробнее."
)


def test_real_provider_keys_never_leak_into_journal(monkeypatch):
    """Полный прогон main.run() на моках сети и SMTP, но с НАСТОЯЩИМИ ключами
    из llm_providers.json: они проходят через llm_writer (заголовки запроса),
    и их значения не должны появиться ни в sent_log.csv, ни в logs/app.log."""
    real_providers = Path(config.BASE_DIR) / "llm_providers.json"
    if not real_providers.exists():
        pytest.skip("llm_providers.json отсутствует")
    monkeypatch.setattr(config, "LLM_PROVIDERS_JSON", real_providers)
    data = json.loads(real_providers.read_text(encoding="utf-8"))
    keys = [p["apiKey"] for p in data.get("providers", []) if p.get("apiKey")]
    assert len(keys) >= 1

    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret-password")
    _write_vacancies([{
        "title": "Тестировщик (QA)",
        "company": "Ромашка",
        "description": "Ручное тестирование веб-приложения",
        "hr_email": "hr@example.com",
    }])
    _close_logging_handlers()
    config.setup_logging()

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        return _FakeResponse({"choices": [{"message": {
            "content": "Отклик на вакансию QA\n\n" + _LONG_BODY}}]})

    monkeypatch.setattr(llm_writer.requests, "post", fake_post)

    code = main.run()
    assert code == 0
    assert len(FakeSMTP.instances) == 1  # письмо прошло весь путь

    journal = Path(config.SENT_LOG_CSV).read_text(encoding="utf-8-sig")
    app_log = Path(config.LOG_FILE).read_text(encoding="utf-8")
    for secret in keys + ["dummy-secret-password"]:
        assert secret not in journal, "ключ провайдера утёк в sent_log.csv"
        assert secret not in app_log, "ключ провайдера утёк в logs/app.log"
    assert "sent" in journal  # строка об отправке записана


# --- ТЗ: черновики в drafts/ ---

def test_draft_saved_with_sanitized_name_and_no_secrets(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret-password")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-deepseek-key")
    _write_vacancies([{
        "title": "Тестировщик (QA)",
        "company": 'ООО "Ромашка": отдел / QA',  # символы, недопустимые в именах файлов
        "description": "Ручное тестирование",
        "hr_email": "hr@example.com",
    }])
    monkeypatch.setattr(llm_writer, "generate_letter",
                        lambda *a, **k: ("Тема письма", "Тело письма"))

    code = main.run()
    assert code == 0
    drafts = list(Path(config.DRAFTS_DIR).glob("*.txt"))
    assert len(drafts) == 1
    name = drafts[0].name
    assert name.startswith("01_")
    for ch in '<>:"/\\|?*':
        assert ch not in name  # имя очищено от недопустимых символов
    text = drafts[0].read_text(encoding="utf-8")
    assert "TO: hr@example.com" in text
    assert "SUBJECT: Тема письма" in text
    assert "BODY:" in text
    assert "Тело письма" in text
    assert "dummy-secret-password" not in text
    assert "dummy-deepseek-key" not in text


def test_draft_saved_even_when_send_fails(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret-password")
    _write_vacancies([{
        "title": "QA",
        "company": "Ромашка",
        "description": "d",
        "hr_email": "hr@example.com",
    }])
    monkeypatch.setattr(llm_writer, "generate_letter",
                        lambda *a, **k: ("Тема", "Тело письма"))
    monkeypatch.setattr(FakeSMTP, "fail_sends", True, raising=False)

    code = main.run()
    assert code == 0  # ошибки отправки не останавливают пакет
    drafts = list(Path(config.DRAFTS_DIR).glob("*.txt"))
    assert len(drafts) == 1  # черновик сохранён ДО отправки, независимо от её результата
    assert "Тело письма" in drafts[0].read_text(encoding="utf-8")


# --- ALLOW_RESEND: временный выключатель блока повторных отправок ---

_PREVIOUSLY_SENT = (
    "datetime,company,vacancy,to_email,status,note\n"
    "2026-11-21 10:00:00,Ромашка,QA,hr@example.com,sent,\n"
)


def _last_status():
    with open(config.SENT_LOG_CSV, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-1]["status"]


def test_block_active_by_default(monkeypatch):
    config.SENT_LOG_CSV.write_text(_PREVIOUSLY_SENT, encoding="utf-8")
    _write_vacancies([{"title": "QA", "company": "Ромашка",
                       "description": "d", "hr_email": "hr@example.com"}])
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret-password")
    monkeypatch.setattr(llm_writer, "generate_letter",
                        lambda *a, **k: ("Тема", "Тело письма"))
    assert config.ALLOW_RESEND is False  # по умолчанию блок включён
    code = main.run()
    assert code == 0
    assert _last_status() == "skipped"       # повтор заблокирован
    assert FakeSMTP.instances == []          # SMTP даже не открывался


def test_allow_resend_bypasses_block(monkeypatch):
    config.SENT_LOG_CSV.write_text(_PREVIOUSLY_SENT, encoding="utf-8")
    _write_vacancies([{"title": "QA", "company": "Ромашка",
                       "description": "d", "hr_email": "hr@example.com"}])
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret-password")
    monkeypatch.setattr(llm_writer, "generate_letter",
                        lambda *a, **k: ("Тема", "Тело письма"))
    monkeypatch.setattr(config, "ALLOW_RESEND", True)  # тестовый режим
    code = main.run()
    assert code == 0
    assert _last_status() == "sent"          # письмо ушло повторно
    assert len(FakeSMTP.instances[0].sent) == 1
