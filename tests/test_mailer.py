"""Моковые тесты mailer: пароль только из окружения, без реального SMTP."""

import smtplib
from pathlib import Path

import pytest

import config
import mailer


class FakeSMTP:
    """Фейковый SMTP_SSL: ничего не отправляет, всё записывает."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.creds = None
        self.sent = []
        self.sent_envelopes = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def login(self, user, password):
        if getattr(self, "auth_fail", False):
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")
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


MSG = {
    "to": "hr@example.com",
    "subject": "Тема письма",
    "body": "Тело письма",
    "vacancy": "QA инженер",
    "company": "Ромашка",
}


@pytest.fixture(autouse=True)
def fake_smtp_env(monkeypatch):
    """Все тесты модуля: фейковый SMTP, паузы = 0, без вложения."""
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(config, "PAUSE_BETWEEN_MIN", 0)
    monkeypatch.setattr(config, "PAUSE_BETWEEN_MAX", 0)
    monkeypatch.setattr(config, "ATTACH_RESUME", False)
    yield
    FakeSMTP.instances = []


# --- пароль только из окружения ---

def test_password_read_from_environment_only(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    results = mailer.send_batch([dict(MSG)])
    assert results[0]["status"] == "sent"
    server = FakeSMTP.instances[0]
    assert server.creds == (config.SENDER_EMAIL, "dummy-secret")
    assert server.host == config.SMTP_HOST
    assert server.port == config.SMTP_PORT
    assert server.quit_called


def test_missing_env_raises_auth_error(monkeypatch):
    monkeypatch.delenv("MAILRU_APP_PASSWORD", raising=False)
    with pytest.raises(mailer.MailAuthError):
        mailer.send_batch([dict(MSG)])
    assert FakeSMTP.instances[0].sent == []


def test_bad_password_stops_whole_run(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "wrong")
    monkeypatch.setattr(FakeSMTP, "auth_fail", True, raising=False)

    def broken_login(self, user, password):
        if self.auth_fail:
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")
        self.creds = (user, password)

    monkeypatch.setattr(FakeSMTP, "login", broken_login)
    with pytest.raises(mailer.MailAuthError):
        mailer.send_batch([dict(MSG), {"to": "other@example.com",
                                       "subject": "s", "body": "b"}])


def test_password_not_hardcoded_in_source():
    source = Path(mailer.__file__).read_text(encoding="utf-8")
    # имя переменной встречается ровно дважды: чтение из окружения и подсказка
    # в сообщении об ошибке. Сам пароль нигде не хардкодится — это проверяют
    # тесты с фейковым окружением.
    lines = [ln for ln in source.splitlines() if "MAILRU_APP_PASSWORD" in ln]
    assert len(lines) == 2
    assert any('os.environ.get("MAILRU_APP_PASSWORD")' in ln for ln in lines)


# --- проверка письма перед отправкой ---

def test_no_email_skipped(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    results = mailer.send_batch([{**MSG, "to": ""}])
    assert results[0]["status"] == "skipped"
    assert results[0]["note"] == "нет email работодателя"
    assert FakeSMTP.instances == []  # соединение даже не открывалось


def test_empty_subject_or_body_skipped(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    results = mailer.send_batch([
        {"to": "a@example.com", "subject": "", "body": "b"},
        {"to": "b@example.com", "subject": "s", "body": ""},
    ])
    assert [r["status"] for r in results] == ["skipped", "skipped"]
    assert FakeSMTP.instances == []


def test_duplicate_address_skipped_within_run(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    results = mailer.send_batch([dict(MSG), dict(MSG)])
    assert [r["status"] for r in results] == ["sent", "skipped"]
    assert results[1]["note"] == "повторный адрес в этом запуске"
    assert len(FakeSMTP.instances[0].sent) == 1


# --- отправка ---

def test_message_headers_and_utf8_body(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    mailer.send_batch([dict(MSG)])
    letter = FakeSMTP.instances[0].sent[0]
    assert letter["From"] == config.SENDER_EMAIL
    assert letter["To"] == "hr@example.com"
    assert letter["Subject"] == "Тема письма"
    plain = letter.get_body(preferencelist=("plain",))
    assert plain.get_content_type() == "text/plain"
    assert plain.get_content_charset() == "utf-8"
    assert plain.get_content() == "Тело письма\n"


def test_send_error_does_not_stop_batch(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")

    def failing_send(self, msg, to_addrs=None):
        raise smtplib.SMTPException("boom")

    monkeypatch.setattr(FakeSMTP, "send_message", failing_send)
    results = mailer.send_batch([
        dict(MSG),
        {"to": "hr2@example.com", "subject": "s", "body": "b"},
    ])
    assert [r["status"] for r in results] == ["error", "error"]
    assert all(r["note"] == "SMTPException" for r in results)


def test_copy_to_self_via_envelope(monkeypatch):
    """Копия себе через SMTP-конверт: работодателю видно только его адрес,
    а ящик mail.ru получает архивную копию (SMTP сам не пишет в «Отправленные»)."""
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    monkeypatch.setattr(config, "SEND_COPY_TO_SELF", True)
    mailer.send_batch([dict(MSG)])
    server = FakeSMTP.instances[0]
    assert server.sent_envelopes == [("hr@example.com", config.SENDER_EMAIL)]
    letter = server.sent[0]
    assert letter["To"] == "hr@example.com"          # в заголовке только работодатель
    assert config.SENDER_EMAIL not in letter["To"]   # копия не видна получателю


def test_copy_to_self_disabled_envelope_is_plain(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    monkeypatch.setattr(config, "SEND_COPY_TO_SELF", False)
    mailer.send_batch([dict(MSG)])
    assert FakeSMTP.instances[0].sent_envelopes == [None]


def test_one_connection_for_whole_batch(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    mailer.send_batch([dict(MSG), {"to": "hr2@example.com", "subject": "s", "body": "b"}])
    assert len(FakeSMTP.instances) == 1  # одно соединение на весь пакет
    assert len(FakeSMTP.instances[0].sent) == 2
    assert FakeSMTP.instances[0].quit_called


def test_on_result_callback_receives_every_result(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    received = []
    mailer.send_batch([dict(MSG), dict(MSG)], on_result=received.append)
    assert len(received) == 2
    assert [r["status"] for r in received] == ["sent", "skipped"]


def test_attachment_added_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(config, "RESUME_PDF", resume)
    monkeypatch.setattr(config, "ATTACH_RESUME", True)
    mailer.send_batch([dict(MSG)])
    letter = FakeSMTP.instances[0].sent[0]
    attachments = [p for p in letter.iter_attachments()]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "resume.pdf"
    assert attachments[0].get_content_type() == "application/pdf"


# --- инвариант From = логин SMTP ---

def test_from_equals_login_in_every_sent_letter(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")
    mailer.send_batch([dict(MSG), {"to": "hr2@example.com", "subject": "s", "body": "b"}])
    assert len(FakeSMTP.instances) == 1
    for letter in FakeSMTP.instances[0].sent:
        assert letter["From"] == config.SMTP_LOGIN
        mailer._ensure_from_is_login(letter)  # не должно бросать


def test_build_message_passes_from_check():
    letter = mailer._build_message("hr@example.com", "Тема", "Тело")
    mailer._ensure_from_is_login(letter)  # не бросает


def test_from_mismatch_raises_config_error():
    letter = mailer._build_message("hr@example.com", "Тема", "Тело")
    letter.replace_header("From", "someone-else@example.com")
    with pytest.raises(mailer.MailConfigError):
        mailer._ensure_from_is_login(letter)


def test_from_mismatch_stops_whole_batch(monkeypatch):
    monkeypatch.setenv("MAILRU_APP_PASSWORD", "dummy-secret")

    def broken_check(msg):
        raise mailer.MailConfigError("From не совпадает с логином SMTP")

    monkeypatch.setattr(mailer, "_ensure_from_is_login", broken_check)
    with pytest.raises(mailer.MailConfigError):
        mailer.send_batch([dict(MSG), {"to": "hr2@example.com", "subject": "s", "body": "b"}])
    # соединение открыто и корректно закрыто, письма не ушли
    assert FakeSMTP.instances[0].quit_called
    assert FakeSMTP.instances[0].sent == []
