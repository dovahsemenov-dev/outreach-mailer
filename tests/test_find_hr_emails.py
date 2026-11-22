"""find_hr_emails: сетевой слой — проверка TLS-сертификата никогда не отключается."""

from unittest import mock

import requests

import find_hr_emails


class _FakeResponse:
    def __init__(self, text="", status_code=200, content_type="text/html; charset=utf-8"):
        self.text = text
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


def test_fetch_returns_text_on_200():
    session = mock.Mock()
    session.get.return_value = _FakeResponse("<html>ok</html>")
    assert find_hr_emails.fetch(session, "https://example.com/vac") == "<html>ok</html>"
    assert session.get.call_args.kwargs["verify"] is True


def test_fetch_retry_keeps_verification():
    """Повторная попытка после сетевой ошибки — тоже с проверкой сертификата."""
    session = mock.Mock()
    session.get.side_effect = [
        requests.exceptions.ConnectionError("boom"),
        _FakeResponse("<html>ok</html>"),
    ]
    assert find_hr_emails.fetch(session, "https://example.com/vac") == "<html>ok</html>"
    verify_flags = [c.kwargs["verify"] for c in session.get.call_args_list]
    assert verify_flags == [True, True]


def test_fetch_empty_on_non_200():
    session = mock.Mock()
    session.get.return_value = _FakeResponse("", status_code=404)
    assert find_hr_emails.fetch(session, "https://example.com/vac") == ""
