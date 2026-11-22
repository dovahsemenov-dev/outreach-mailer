"""Гварды git-гигиены: секретные файлы не в индексе и покрыты .gitignore."""

import subprocess
from pathlib import Path

import pytest

import config

PROJECT_DIR = Path(config.BASE_DIR)

SECRET_PATHS = ["llm_providers.json", ".env", "sent_log.csv", "resume.pdf"]


def _run_git(args):
    return subprocess.run(["git", *args], cwd=PROJECT_DIR, capture_output=True, text=True)


def _in_git_repo():
    return _run_git(["rev-parse", "--git-dir"]).returncode == 0


@pytest.mark.skipif(not _in_git_repo(), reason="проект вне git-репозитория")
def test_secret_files_not_in_git_index():
    tracked = set(_run_git(["ls-files", "--full-name"]).stdout.splitlines())
    for name in SECRET_PATHS:
        full = "outreach-mailer/" + name
        assert full not in tracked, (
            "{} закоммичен в git — выполни 'git rm --cached {}' и добавь файл в .gitignore".format(full, full)
        )


@pytest.mark.skipif(not _in_git_repo(), reason="проект вне git-репозитория")
def test_llm_providers_json_is_gitignored():
    """Именно llm_providers.json должен быть покрыт .gitignore — это защита
    от случайного 'git add .' с реальными ключами."""
    result = _run_git(["check-ignore", "-v", "llm_providers.json"])
    assert result.returncode == 0, "llm_providers.json не покрыт .gitignore"
    assert "llm_providers.json" in result.stdout  # правило найдено
