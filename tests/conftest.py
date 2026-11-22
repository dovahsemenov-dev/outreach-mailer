"""Подключает корень проекта к sys.path, чтобы тесты импортировали модули проекта."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest


@pytest.fixture(autouse=True)
def _test_inputs(monkeypatch):
    """Тесты не зависят от личных файлов (их нет в репозитории):
    фиктивный ключ LLM в окружении + временные файлы профиля рядом с main.py
    (удаляются после теста). Тесты, которым нужен статус «ключей нет»,
    удаляют переменную сами."""
    import config

    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-deepseek-key")
    letter = config.BASE_DIR / "profile_letters.txt"
    base = config.BASE_DIR / "profile.txt"
    profile_text = "УРОВЕНЬ\nТестовый профиль для тестов.\n"
    letter.write_text(profile_text, encoding="utf-8")
    base.write_text(profile_text, encoding="utf-8")
    yield
    for path in (letter, base):
        try:
            path.unlink()
        except OSError:
            pass
