"""Тесты веб-панели: валидация настроек/компаний, обновление .env без утечек."""

import pytest

import panel


# --- валидация настроек ---

def test_validate_settings_ok():
    clean, errors = panel.validate_settings({
        "max_per_run": "5",
        "pause_min": "20", "pause_max": "60",
        "attach_resume": True, "allow_resend": False,
    })
    assert errors == []
    assert clean == {
        "max_per_run": 5,
        "pause_min": 20, "pause_max": 60,
        "attach_resume": True, "send_copy_to_self": True, "allow_resend": False,
    }


def test_validate_settings_int_bounds():
    clean, errors = panel.validate_settings({"max_per_run": "500", "pause_min": "3"})
    assert len(errors) == 2
    assert clean["max_per_run"] == panel._DEFAULT_SETTINGS["max_per_run"]
    assert clean["pause_min"] == panel._DEFAULT_SETTINGS["pause_min"]


def test_validate_settings_pause_order_swapped():
    clean, errors = panel.validate_settings({"pause_min": "60", "pause_max": "20"})
    assert errors and "минимум больше" in errors[0]
    assert clean["pause_min"] == 20 and clean["pause_max"] == 60


def test_validate_settings_allow_resend_warns():
    _, errors = panel.validate_settings({"allow_resend": True})
    assert any("ALLOW_RESEND" in e for e in errors)


def test_validate_settings_never_raises_on_garbage():
    clean, errors = panel.validate_settings({"max_per_run": "abc", "pause_min": None})
    assert isinstance(clean, dict) and isinstance(errors, list)


# --- валидация компаний ---

def test_validate_vacancies_ok_and_fields_stripped():
    clean, errors = panel.validate_vacancies([
        {"title": " QA ", "company": " Ромашка ", "description": " x ",
         "hr_email": " hr@romashka.ru "},
    ])
    assert errors == []
    assert clean == [{"title": "QA", "company": "Ромашка", "description": "x",
                      "hr_email": "hr@romashka.ru"}]


def test_validate_vacancies_bad_email_and_empty_title():
    clean, errors = panel.validate_vacancies([
        {"title": "QA", "hr_email": "не-почта"},
        {"title": "", "hr_email": "a@b.ru"},
        {"company": "без всего"},
    ])
    assert len(errors) == 3
    assert clean == []


def test_validate_vacancies_dedup_addresses():
    clean, errors = panel.validate_vacancies([
        {"title": "QA", "company": "A", "hr_email": "hr@x.ru"},
        {"title": "QA", "company": "B", "hr_email": "HR@X.RU"},  # тот же адрес
    ])
    assert len(clean) == 1
    assert any("дубликат" in e for e in errors)


def test_validate_vacancies_never_raises_on_garbage():
    clean, errors = panel.validate_vacancies(["строка", 42, None])
    assert clean == [] and len(errors) == 3


# --- .env: обновление без утечек ---

def test_update_env_preserves_other_lines(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MAILRU_APP_PASSWORD=old-secret\n"
        "# комментарий\n"
        "DEEPSEEK_API_KEY=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(panel, "ENV_FILE", env_file)

    status = panel.update_env({"MAILRU_APP_PASSWORD": " new-secret "})
    text = env_file.read_text(encoding="utf-8")
    assert "# комментарий" in text                       # чужие строки целы
    assert "MAILRU_APP_PASSWORD=new-secret" in text      # значение заменено
    assert "old-secret" not in text                      # старое затёрто
    assert status["mailru_set"] is True
    assert "new-secret" not in str(status)               # значение не возвращается


def test_update_env_empty_value_means_no_change(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MAILRU_APP_PASSWORD=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_FILE", env_file)
    panel.update_env({"MAILRU_APP_PASSWORD": "", "DEEPSEEK_API_KEY": None})
    assert "keep-me" in env_file.read_text(encoding="utf-8")


def test_update_env_creates_missing_key(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# пусто\n", encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_FILE", env_file)
    status = panel.update_env({"DEEPSEEK_API_KEY": "sk-test"})
    text = env_file.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-test" in text
    assert status["deepseek_set"] is True


def test_env_status_never_exposes_values(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MAILRU_APP_PASSWORD=top-secret-123\n", encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_FILE", env_file)
    status = panel.env_status()
    assert status == {"mailru_set": True, "deepseek_set": False}
    assert "top-secret-123" not in str(status)


def test_state_has_no_env_values(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MAILRU_APP_PASSWORD=top-secret-123\n", encoding="utf-8")
    monkeypatch.setattr(panel, "ENV_FILE", env_file)
    monkeypatch.setattr(panel, "SETTINGS_FILE", tmp_path / "no_settings.json")
    monkeypatch.setattr(panel, "VACANCIES_FILE", tmp_path / "no_vacancies.json")
    monkeypatch.setattr(panel, "SENT_LOG_FILE", tmp_path / "no_log.csv")
    state = panel.state()
    assert "top-secret-123" not in str(state)
    assert state["env"]["mailru_set"] is True


# --- запуск бота из панели ---

def test_logtail_reads_last_lines(monkeypatch, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text("a\nb\nc\n", encoding="utf-8")
    monkeypatch.setattr(panel, "LOG_FILE", log_file)
    monkeypatch.setattr(panel, "LOCK_FILE", tmp_path / "bot.lock")
    res = panel.logtail()
    assert res["running"] is False
    assert res["tail"] == ["a", "b", "c"]


def test_run_bot_refused_when_already_running(monkeypatch):
    monkeypatch.setattr(panel, "bot_running", lambda: True)
    spawned = []
    monkeypatch.setattr(panel.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a))
    res = panel.run_bot()
    assert res["ok"] is False
    assert spawned == []


def test_run_bot_spawns_main_py_detached(monkeypatch, tmp_path):
    monkeypatch.setattr(panel, "bot_running", lambda: False)
    monkeypatch.setattr(panel, "LOG_FILE", tmp_path / "logs" / "app.log")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr(panel.subprocess, "Popen", fake_popen)
    res = panel.run_bot()
    assert res["ok"] is True
    assert captured["cmd"][0] == panel.sys.executable
    assert str(panel.MAIN_PY) in captured["cmd"]
    assert captured["kwargs"]["cwd"] == str(panel.BASE_DIR)
    assert captured["kwargs"]["creationflags"] > 0  # отсоединённый процесс


def test_bot_running_false_without_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(panel, "LOCK_FILE", tmp_path / "no.lock")
    assert panel.bot_running() is False


def test_bot_running_clears_dead_lock(monkeypatch, tmp_path):
    """Замок от крашнувшегося процесса подчищается автоматически."""
    lock = tmp_path / "bot.lock"
    lock.write_text("999999999", encoding="utf-8")  # такого PID нет
    monkeypatch.setattr(panel, "LOCK_FILE", lock)
    assert panel.bot_running() is False
    assert not lock.exists()
