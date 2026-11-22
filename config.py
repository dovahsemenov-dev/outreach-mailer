"""Константы проекта. Секретов здесь нет и быть не должно:
пароли и ключи читаются только из переменных окружения (см. README).

Часть настроек можно менять через веб-панель (настройки_бота.bat): она пишет
settings.json, а этот конфиг при запуске применяет значения поверх дефолтов."""

import json
import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path=None):
    """Подтягивает файл .env (если он есть) в os.environ, не перезаписывая
    уже заданные переменные. Значения никуда не выводятся и не логируются."""
    env_path = path or BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


load_env_file()


def _load_panel_settings():
    """Читает settings.json (его пишет веб-панель). Без файла - дефолты."""
    path = BASE_DIR / "settings.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("ВНИМАНИЕ: settings.json не прочитан ({}), работаю на дефолтах".format(
            type(exc).__name__))
        return {}
    return data if isinstance(data, dict) else {}


PANEL_SETTINGS = _load_panel_settings()

# --- SMTP (Mail.ru) ---
SMTP_HOST = "smtp.mail.ru"
SMTP_PORT = 465                          # SMTP_SSL, без STARTTLS
SMTP_TIMEOUT = 30                        # секунд
SENDER_EMAIL = "your_login@mail.ru"      # [ЛИЧНЫЕ ДАННЫЕ] впиши свой адрес отправителя (From)
SMTP_LOGIN = SENDER_EMAIL                # логин SMTP — инвариант: должен совпадать с From
CANDIDATE_NAME = "Имя"                   # [ЛИЧНЫЕ ДАННЫЕ] имя кандидата для писем (в profile.txt его нет)
PROFILE_MAX_CHARS = 30000                # сколько символов профиля видеть модели
                                         # (весь profile.txt ~22к символов — режем с запасом)

# Сильные тезисы, которые ОБЯЗАТЕЛЬНО звучат в каждом письме
# (всё подтверждено фактами в profile.txt)
HIGHLIGHTS = [
    "писал end-to-end автотесты с полным сценарием и внедрял их в Jenkins",
]

# --- LLM (DeepSeek и совместимые провайдеры) ---
# Основной источник ключей — файл llm_providers.json (бесплатные прокси-роутеры
# с моделью DeepSeek-V4.1-Flash). Имя файла можно переопределить в .env
# переменной LLM_PROVIDERS_JSON. Если файла нет — используется один провайдер:
# официальный API из переменной окружения DEEPSEEK_API_KEY.
_providers_name = os.environ.get("LLM_PROVIDERS_JSON", "llm_providers.json")
_providers_path = Path(_providers_name)
LLM_PROVIDERS_JSON = _providers_path if _providers_path.is_absolute() else BASE_DIR / _providers_name

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# Точное имя модели официального API, сверено с api-docs.deepseek.com:
# идентификатор "deepseek-flash" (обслуживается DeepSeek-V4.1-Flash-1017).
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_TIMEOUT = 60                    # секунд
BODY_WORDS_RANGE = (80, 150)             # целевой объём тела письма, слов

# --- лимиты отправки (значения перекрываются settings.json от веб-панели) ---
MAX_PER_RUN = int(PANEL_SETTINGS.get("max_per_run", 30))      # писем за один запуск
PAUSE_BETWEEN_MIN = int(PANEL_SETTINGS.get("pause_min", 20))  # пауза, сек (случайная)
PAUSE_BETWEEN_MAX = int(PANEL_SETTINGS.get("pause_max", 60))
ALLOW_RESEND = bool(PANEL_SETTINGS.get("allow_resend", False))  # ТЕСТОВЫЙ режим:
                                         # True отключает блок повторных отправок.
                                         # Только для тестов на СОБСТВЕННЫХ адресах!
SEND_COPY_TO_SELF = bool(PANEL_SETTINGS.get("send_copy_to_self", True))  # скрытая
                                         # копия каждого письма себе (SMTP-конверт):
                                         # архив отправленного во «Входящих» mail.ru,
                                         # работодатель её не видит. IMAP не используется.

# --- файлы ---
PROFILE_TXT = BASE_DIR / "profile.txt"               # полная база фактов (справочник)

PROFILE_LETTERS_TXT = BASE_DIR / "profile_letters.txt"  # факты для писем
VACANCIES_JSON = BASE_DIR / "vacancies.json"
SENT_LOG_CSV = BASE_DIR / "sent_log.csv"
# [ЛИЧНЫЕ ДАННЫЕ] profile.txt, profile_letters.txt и resume.pdf содержат
# данные кандидата и в репозиторий не публикуются (см. *.example и README).
RESUME_PDF = BASE_DIR / "resume.pdf"
ATTACH_RESUME = bool(PANEL_SETTINGS.get("attach_resume", True))  # прикреплять резюме
LOG_FILE = BASE_DIR / "logs" / "app.log"
DRAFTS_DIR = BASE_DIR / "drafts"         # копии сгенерированных писем до отправки
LOCK_FILE = BASE_DIR / "bot.lock"        # анти-дубль: живёт, пока бот работает

# --- журнал отправок ---
SENT_LOG_FIELDS = ["datetime", "company", "vacancy", "to_email", "status", "note"]


def setup_logging():
    """Логи: консоль + logs/app.log. Значения переменных окружения не логируются."""
    LOG_FILE.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
