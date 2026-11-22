"""Проверки llm_writer: изоляция от почты и секретов, ротация провайдеров, нормализация."""

import ast
import json
from pathlib import Path

import pytest

import config
import llm_writer

SOURCE = Path(llm_writer.__file__).read_text(encoding="utf-8")

SUBJECT = "Отклик на вакансию QA инженер"
BODY = (
    "Здравствуйте! Меня заинтересовала ваша вакансия: у меня есть коммерческий "
    "опыт ручного тестирования, я оформлял тест-кейсы, чек-листы и баг-репорты, "
    "работал с автоматизированными тестами и поддерживал их при изменениях в "
    "продукте. Буду рад обсудить задачу подробнее."
)


@pytest.fixture(autouse=True)
def _isolated_llm(monkeypatch, tmp_path):
    """Тесты живут без реального файла провайдеров и без реальных ключей."""
    monkeypatch.setattr(config, "LLM_PROVIDERS_JSON", tmp_path / "llm_providers.json")
    monkeypatch.setattr(llm_writer, "_provider_offset", 0)
    monkeypatch.setattr(llm_writer, "_provider_empty_counts", {})
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _ok_response(content):
    return _FakeResponse({"choices": [{"message": {"content": content}}]})


def _patch_api(monkeypatch, responses, calls):
    """Подменяет requests.post на фейк, который отдаёт ответы по очереди
    (последний — если ответы закончились)."""
    counter = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        calls.append({"url": url, "json": json, "headers": headers, "verify": verify})
        idx = min(counter["n"], len(responses) - 1)
        counter["n"] += 1
        return responses[idx]

    monkeypatch.setattr(llm_writer.requests, "post", fake_post)
    return counter


def _write_providers(path, providers):
    path.write_text(json.dumps({"providers": providers}), encoding="utf-8")


# --- изоляция от почты и секретов ---

def test_does_not_import_smtplib():
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name != "smtplib" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "smtplib"


def test_source_does_not_reference_mail_password_env():
    assert "MAILRU_APP_PASSWORD" not in SOURCE


def test_prompt_contains_no_smtp_data(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    result = llm_writer.generate_letter(
        "QA инженер", "Ромашка", "Требования: ручное тестирование", "Профиль: junior QA"
    )
    assert result is not None
    prompt_text = json.dumps(calls[0]["json"], ensure_ascii=False)
    assert config.SENDER_EMAIL not in prompt_text       # адрес отправителя
    assert config.SMTP_HOST not in prompt_text          # хост
    assert "465" not in prompt_text                     # порт
    assert "MAILRU_APP_PASSWORD" not in prompt_text     # имя почтовой переменной


def test_api_key_sent_in_header_only(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль")
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"


def test_api_key_not_in_log(monkeypatch, caplog):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_FakeResponse({"error": {"message": "no"}}, 500),
                             _FakeResponse({"error": {"message": "no"}}, 500)], calls)
    with caplog.at_level("WARNING"):
        assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is None
    assert "test-key" not in caplog.text


# --- генерация, повторы, ротация ---

def test_generate_letter_returns_subject_and_body(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    result = llm_writer.generate_letter("QA инженер", "Ромашка", "Описание", "Профиль")
    assert result == (SUBJECT, BODY)


def test_empty_answer_retried_once_then_skipped(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(""), _ok_response("")], calls)
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is None
    assert len(calls) == 2  # исходная попытка + один повтор, затем пропуск


def test_server_error_retried_once_then_skipped(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_FakeResponse({"error": {"message": "boom"}}, 500),
                             _FakeResponse({"error": {"message": "boom"}}, 500)], calls)
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is None
    assert len(calls) == 2


def test_fatal_status_skips_provider_immediately(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_FakeResponse({"error": {"message": "bad key"}}, 401)], calls)
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is None
    assert len(calls) == 1  # 401 не ретраится — ключ не починится за секунду


def test_second_attempt_succeeds(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(""), _ok_response(SUBJECT + "\n\n" + BODY)], calls)
    result = llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль")
    assert result == (SUBJECT, BODY)
    assert len(calls) == 2


def test_missing_keys_skips_without_request(monkeypatch):
    calls = []
    _patch_api(monkeypatch, [], calls)
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is None
    assert len(calls) == 0


def test_has_providers_detects_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    assert llm_writer.has_providers() is True
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert llm_writer.has_providers() is False
    _write_providers(config.LLM_PROVIDERS_JSON, [
        {"id": "p1", "baseUrl": "https://x.example/v1", "apiKey": "k1",
         "model": "m1", "enabled": True},
    ])
    assert llm_writer.has_providers() is True


# --- файл провайдеров: ротация, disabled, TLS ---

def test_fatal_failure_rotates_to_next_provider(monkeypatch):
    _write_providers(config.LLM_PROVIDERS_JSON, [
        {"id": "dead", "baseUrl": "https://dead.example/v1", "apiKey": "k-dead",
         "model": "m", "enabled": True},
        {"id": "alive", "baseUrl": "https://alive.example/v1", "apiKey": "k-alive",
         "model": "m", "enabled": True},
    ])
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        calls.append({"url": url, "headers": headers})
        if "dead" in url:
            return _FakeResponse({"error": {"message": "limit"}}, 429)
        return _ok_response(SUBJECT + "\n\n" + BODY)

    monkeypatch.setattr(llm_writer.requests, "post", fake_post)
    result = llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль")
    assert result == (SUBJECT, BODY)
    assert [c["url"] for c in calls] == [
        "https://dead.example/v1/chat/completions",
        "https://alive.example/v1/chat/completions",
    ]
    assert calls[1]["headers"]["Authorization"] == "Bearer k-alive"


def test_round_robin_advances_after_success(monkeypatch):
    _write_providers(config.LLM_PROVIDERS_JSON, [
        {"id": "p1", "baseUrl": "https://p1.example/v1", "apiKey": "k1",
         "model": "m", "enabled": True},
        {"id": "p2", "baseUrl": "https://p2.example/v1", "apiKey": "k2",
         "model": "m", "enabled": True},
    ])
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль")
    llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль")
    assert [c["url"] for c in calls] == [
        "https://p1.example/v1/chat/completions",
        "https://p2.example/v1/chat/completions",
    ]


def test_disabled_providers_are_skipped(monkeypatch):
    _write_providers(config.LLM_PROVIDERS_JSON, [
        {"id": "off", "baseUrl": "https://off.example/v1", "apiKey": "k",
         "model": "m", "enabled": False},
        {"id": "on", "baseUrl": "https://on.example/v1", "apiKey": "k",
         "model": "m", "enabled": True},
    ])
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is not None
    assert [c["url"] for c in calls] == ["https://on.example/v1/chat/completions"]


def test_tls_verification_on_by_default(monkeypatch):
    _write_providers(config.LLM_PROVIDERS_JSON, [
        {"id": "p1", "baseUrl": "https://p1.example/v1", "apiKey": "k1",
         "model": "m", "enabled": True},
    ])
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is not None
    assert calls[0]["verify"] is True


# --- правила вкуса в промпте ---

def test_prompt_bans_trivial_and_training_facts():
    """Промпт обязан запрещать азы, учебные песочницы, испытательный срок
    и упоминание слабых сторон — в обоих режимах."""
    prompt = (llm_writer.SYSTEM_PROMPT + llm_writer.USER_PROMPT_TEMPLATE
              + llm_writer.USER_PROMPT_COLD_TEMPLATE)
    for banned_hint in ("severity", "испытательн", "JSON Server",
                        "N объектов", "умалчи", "Postman", "инициативн",
                        "хочу работать в вашей", "обсудить задачи"):
        assert banned_hint in prompt


def test_cold_mode_prompt_when_description_empty(monkeypatch):
    """Без описания вакансии — холодный режим: письмо выглядит как обычный
    отклик на вакансию, тема строго «Отклик на вакансию ...»."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    result = llm_writer.generate_letter("QA-тестировщик", "Ромашка", "", "Профиль")
    assert result == (SUBJECT, BODY)
    prompt_text = json.dumps(calls[0]["json"], ensure_ascii=False)
    assert "ОБЫЧНЫЙ отклик" in prompt_text
    assert "строго «Отклик на вакансию QA-тестировщик»" in prompt_text


def test_subject_with_banned_words_replaced_by_fallback(monkeypatch):
    """Кривая тема модели («Инициативное письмо...») заменяется на запасную."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    bad = "Инициативное письмо QA-тестировщика"
    _patch_api(monkeypatch, [_ok_response(bad + "\n\n" + BODY)], calls)
    subject, body = llm_writer.generate_letter("QA-тестировщик", "Ромашка", "", "Профиль")
    assert subject == "Отклик на вакансию QA-тестировщик"
    assert body == BODY


def test_vacancy_mode_prompt_when_description_present(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    llm_writer.generate_letter("QA", "Ромашка", "Ручное тестирование", "Профиль")
    prompt_text = json.dumps(calls[0]["json"], ensure_ascii=False)
    assert "задачи и требования" in prompt_text  # вакансионный шаблон
    assert "инициативное письмо" not in prompt_text


def test_candidate_name_passed_to_prompt(monkeypatch):
    """Имя кандидата попадает в оба режима — иначе модель пишет 'Меня зовут —'."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    for description in ("Ручное тестирование", ""):
        calls = []
        _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
        llm_writer.generate_letter("QA", "Ромашка", description, "Профиль")
        prompt_text = json.dumps(calls[0]["json"], ensure_ascii=False)
        assert config.CANDIDATE_NAME in prompt_text
        assert "Меня зовут —" in prompt_text  # запрет прочерка вместо имени


def test_profile_letters_configured():
    """Конфиг указывает на файл фактов для писем рядом с main.py."""
    assert config.PROFILE_LETTERS_TXT == config.BASE_DIR / "profile_letters.txt"
    assert config.RESUME_PDF == config.BASE_DIR / "resume.pdf"


def test_system_prompt_is_mode_neutral():
    """Уровень кандидата задаёт файл профиля, а не системный промпт."""
    assert "с коммерческим опытом" not in llm_writer.SYSTEM_PROMPT


def test_attachment_line_switches_with_flag(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль", attach_resume=True)
    with_attachment = json.dumps(calls[0]["json"], ensure_ascii=False)
    calls.clear()
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль")
    without_attachment = json.dumps(calls[0]["json"], ensure_ascii=False)
    assert "во вложении" in with_attachment
    assert "во вложении" not in without_attachment  # без вложения фразы нет,
    # есть только строка-запрет "никаких упоминаний вложений..."


# --- нормализация ответа модели ---

def test_normalize_strips_markdown_and_prefix():
    raw = "```\n**Тема:** Отклик на вакансию\n\n# Привет\n\n\n\nТекст письма.\n```"
    out = llm_writer._normalize(raw)
    assert "```" not in out
    assert "**" not in out
    assert out.startswith("Отклик")


def test_normalize_strips_quotes():
    assert llm_writer._normalize('"Отклик на вакансию"') == "Отклик на вакансию"
    assert llm_writer._normalize("«Отклик на вакансию»") == "Отклик на вакансию"


def test_normalize_replaces_long_dash_with_hyphen():
    """Длинное тире — почерк ИИ; остаётся короткий дефис."""
    assert llm_writer._normalize("опыт — важное") == "опыт - важное"
    assert llm_writer._normalize("тесты – быстро") == "тесты - быстро"


def test_prompt_includes_highlights_and_full_profile(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    _patch_api(monkeypatch, [_ok_response(SUBJECT + "\n\n" + BODY)], calls)
    llm_writer.generate_letter("QA", "Ромашка", "", "Профиль")
    prompt_text = json.dumps(calls[0]["json"], ensure_ascii=False)
    for thesis in config.HIGHLIGHTS:
        assert thesis in prompt_text          # тезисы E2E/Jenkins обязательны
    assert "ВСЕ рабочие инструменты" in prompt_text  # холодный режим: весь стек


def test_freeform_answer_treated_as_body(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []
    # нет пустой строки после первой строки — считается, что темы нет
    _patch_api(monkeypatch, [_ok_response(BODY)], calls)
    subject, body = llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль")
    assert subject == "Отклик на вакансию QA"  # запасная тема
    assert body == BODY


def test_mid_sentence_answer_is_rejected_and_retried(monkeypatch):
    """Оборванный посреди фразы ответ — брак: повтор, потом успех."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    truncated = SUBJECT + "\n\n" + " ".join(["слово"] * 30) + " чтобы"
    calls = []
    _patch_api(monkeypatch, [_ok_response(truncated),
                             _ok_response(SUBJECT + "\n\n" + BODY)], calls)
    result = llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль")
    assert result == (SUBJECT, BODY)
    assert len(calls) == 2


def test_mid_sentence_all_attempts_fail_returns_none(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    truncated = SUBJECT + "\n\n" + " ".join(["слово"] * 30) + " чтобы"
    calls = []
    _patch_api(monkeypatch, [_ok_response(truncated), _ok_response(truncated)], calls)
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is None
    assert len(calls) == 2


def test_provider_with_repeated_empty_answers_gets_skipped(monkeypatch):
    """Провайдер, набравший порог брака, пропускается в этом запуске:
    бесплатный прокси, душащий пустыми ответами, не съедает время."""
    _write_providers(config.LLM_PROVIDERS_JSON, [
        {"id": "flaky", "baseUrl": "https://flaky.example/v1", "apiKey": "k1",
         "model": "m", "enabled": True},
        {"id": "good", "baseUrl": "https://good.example/v1", "apiKey": "k2",
         "model": "m", "enabled": True},
    ])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        calls.append({"url": url})
        if "flaky" in url:
            return _ok_response("")  # этот провайдер всегда бракует
        return _ok_response(SUBJECT + "\n\n" + BODY)

    monkeypatch.setattr(llm_writer.requests, "post", fake_post)

    # счётчик брака уже на пороге — flaky пропускается целиком
    monkeypatch.setattr(llm_writer, "_provider_empty_counts", {"flaky": 4})
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is not None
    assert [c["url"] for c in calls] == ["https://good.example/v1/chat/completions"]

    # пока счётчик ниже порога — провайдер участвует (и опять бракует)
    monkeypatch.setattr(llm_writer, "_provider_empty_counts", {"flaky": 2})
    calls.clear()
    assert llm_writer.generate_letter("QA", "Ромашка", "Описание", "Профиль") is not None
    assert sum(1 for c in calls if "flaky" in c["url"]) == 2  # две попытки
    assert sum(1 for c in calls if "good" in c["url"]) == 1   # good спасает письмо
