"""Генерация темы и тела письма через LLM (DeepSeek и совместимые провайдеры).

Модуль занимается только текстом письма: он не отправляет почту, не знает
о SMTP-настройках и не читает почтовые секреты из окружения.

Ключи провайдеров лежат в llm_providers.json (в .gitignore) либо, если файла
нет, в переменной окружения DEEPSEEK_API_KEY. Провайдеры перебираются по кругу:
упёрся в лимит / ключ не принят / ошибка сети — берётся следующий.
"""

import json
import logging
import os
import re

import requests
import urllib3

import config

# часть бесплатных роутеров отвечает с просроченным сертификатом
# (allowExpiredTls в исходном конфиге) — глушим предупреждения urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_MIN_BODY_WORDS = 25  # sanity-порог: короче — ответ считается ошибочным
# Статусы, при которых повторять этого же провайдера бессмысленно:
# плохой ключ, нет квоты, доступ закрыт, упор в лимит — сразу следующий.
_FATAL_STATUSES = {401, 402, 403, 429}

_provider_offset = 0  # с какого провайдера начинать следующий запрос (круговой обход)
_provider_empty_counts = {}  # провайдер -> счётчик бракованных (пустых/оборванных) ответов
_EMPTY_SKIP_THRESHOLD = 4    # столько брака за запуск — провайдер пропускается
                             # (бесплатные прокси душат пустыми ответами при троттлинге)

SYSTEM_PROMPT = (
    "Ты пишешь сопроводительные письма кандидата QA работодателям в ответ "
    "на вакансии.\n"
    "Правила вкуса:\n"
    "- не перечисляй азы: виды тестирования, техники тест-дизайна, шаги "
    "воспроизведения, severity и priority, оформление баг-репортов — это "
    "очевидно любому тестировщику и в письме не упоминается;\n"
    "- из профиля бери только сильные факты, уместные именно для этой вакансии; "
    "учебную и самостоятельную практику, песочницы и тренировочные кейсы "
    "(JSON Server, FakeRESTApi, любые счётчики вида 'отправлял N объектов') "
    "не упоминать никогда;\n"
    "- Postman и DevTools не упоминать ВООБЩЕ: знание этих инструментов "
    "подразумевается у любого QA и в письме выглядит как азы;\n"
    "- не придумывай ничего, чего нет в профиле; если данных мало — пиши короче;\n"
    "- слабые стороны умалчивай: никаких 'пока не работал с', 'новое для меня "
    "направление', извинений и оправданий;\n"
    "- тон вежливой сдержанности: начало письма мягкое — 'направляю своё "
    "резюме', 'хотел бы предложить свою кандидатуру', 'буду рад, если вы "
    "ознакомитесь с моим резюме'. ЗАПРЕЩЕНО начинать с 'хочу работать в вашей "
    "компании' — звучит бесцеремонно. Концовка: 'буду рад обсудить детали', "
    "'готов ответить на вопросы', 'прошу рассмотреть резюме'. ЗАПРЕЩЕНА "
    "концовка про 'обсудить задачи команды' — кандидата ещё не наняли и "
    "задач у него пока нет;\n"
    "- не упоминай испытательный срок, зарплату и условия работы;\n"
    "- без шаблонных фраз вроде 'уважаемые коллеги', без markdown, списков и эмодзи;\n"
    "- от первого лица, по-русски, живым деловым языком, уверенно и по делу; "
    "не упоминай, что текст сгенерирован;\n"
    "- в профиле нет имени кандидата — оно передаётся отдельно в запросе. "
    "Представляйся только этим именем; ЗАПРЕЩЕНО оставлять прочерк или "
    "пустое место вместо имени (вариант 'Меня зовут — кандидат' — это брак);\n"
    "- тема письма ВСЕГДА формата «Отклик на вакансию <должность>», например "
    "«Отклик на вакансию QA-тестировщик». ЗАПРЕЩЕНЫ темы со словами "
    "'инициативное', 'знакомство', 'предложение о сотрудничестве', "
    "'резюме на рассмотрение' и имя кандидата в теме."
)

USER_PROMPT_TEMPLATE = """Напиши сопроводительное письмо на эту вакансию.

Кандидат: {candidate_name}
Вакансия:
- название: {title}
- компания: {company}
- задачи и требования: {description}

Профиль кандидата (единственный источник фактов):
{profile}

Требования к письму:
- объём тела письма: {min_words}-{max_words} слов;
- выбери из профиля 2-3 факта, которые сильнее всего отвечают конкретным
  требованиям ЭТОЙ вакансии, и раскрой их короткими конкретными фразами;
- обязательно отрази тезисы: {highlights};
- {attachment_line}
- закончи одним коротким предложением (готовность рассказать подробнее или
  ответить на вопросы), без заискивания и лишних обещаний;
- не пересказывай профиль целиком и не сваливай всё в один абзац навыков.

Формат ответа (без пояснений):
первая строка — тема письма (коротко, без слова "тема"),
второй строкой — пустая строка,
дальше — текст письма."""

USER_PROMPT_COLD_TEMPLATE = """Напиши отклик на вакансию {title} в компанию.
Кандидат пишет в компанию, где открыты или регулярно открываются QA-вакансии.
Письмо должно выглядеть как ОБЫЧНЫЙ отклик на вакансию: без оговорок о том,
что конкретную вакансию кандидат не видел, без слова «инициативное», без
описания процесса поиска работы.

Кандидат: {candidate_name}
Компания: {company}
Должность: {title}

Профиль кандидата (единственный источник фактов):
{profile}

Требования к письму:
- тема письма: строго «Отклик на вакансию {title}», без других вариантов;
- объём тела письма: {min_words}-{max_words} слов;
- суть: хочу работать в этой компании по направлению тестирования;
  подай 2-3 сильных факта из профиля уверенно, через пользу для команды;
- перечисли ВСЕ рабочие инструменты из профиля (Jira, TestIT, Allure,
  Swagger, Jenkins, Linux, Git, SQL и другие, что там названы), вплетая
  их во фразы, а не отдельным списком;
- обязательно отрази тезисы: {highlights};
- слабые стороны умалчи: никаких «новых направлений», «пока не работал с»,
  извинений и оправданий;
- азы не перечислять; учебные песочницы и тренировочные кейсы — никогда;
- Postman и DevTools не упоминать вовсе;
- не упоминай испытательный срок, зарплату и условия работы;
- закончи одним коротким предложением про рассмотрение резюме;
- {attachment_line}

Формат ответа (без пояснений):
первая строка — тема письма,
второй строкой — пустая строка,
дальше — текст письма."""


class LLMError(Exception):
    """Ошибка генерации. Секретов в сообщении нет."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _load_providers():
    """Собирает список провайдеров: llm_providers.json, а если его нет —
    один провайдер из переменной окружения DEEPSEEK_API_KEY (официальный API)."""
    providers = []
    path = config.LLM_PROVIDERS_JSON
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logging.error("Не удалось прочитать %s: %s", path.name, type(exc).__name__)
            data = {}
        for entry in data.get("providers", []):
            if not entry.get("enabled", True) or not entry.get("apiKey"):
                continue
            providers.append({
                "id": entry.get("id") or entry.get("name") or "provider",
                "base_url": (entry.get("baseUrl") or "").rstrip("/"),
                "model": entry.get("model") or "",
                "api_key": entry["apiKey"],
                # allowExpiredTls из исходного конфига = отключить проверку TLS
                "verify_tls": not entry.get("allowExpiredTls", False),
            })
    if not providers:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if api_key:
            providers.append({
                "id": "deepseek-api",
                "base_url": config.DEEPSEEK_BASE_URL,
                "model": config.DEEPSEEK_MODEL,
                "api_key": api_key,
                "verify_tls": True,
            })
    return providers


def has_providers():
    """Есть ли хоть один рабочий источник ключей."""
    return bool(_load_providers())


def _call_provider(provider, prompt):
    """Один запрос к провайдеру. Возвращает сырой текст ответа или бросает LLMError."""
    payload = {
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 800,
    }
    headers = {
        "Authorization": "Bearer " + provider["api_key"],
        "Content-Type": "application/json",
    }
    if not provider["verify_tls"]:
        logging.warning("%s: проверка TLS-сертификата отключена (allowExpiredTls)",
                        provider["id"])
    resp = requests.post(
        provider["base_url"] + "/chat/completions",
        json=payload,
        headers=headers,
        timeout=config.DEEPSEEK_TIMEOUT,
        verify=provider["verify_tls"],
    )
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        raise LLMError(
            "код {}: {}".format(resp.status_code, detail[:200]),
            status_code=resp.status_code,
        )
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError):
        raise LLMError("неожиданный формат ответа")
    return content or ""


def _normalize(text):
    """Убирает markdown-ограды, жирные маркеры, префикс 'Тема:', лишние
    кавычки по краям, пустые строки и длинные тире (почерк ИИ) —
    остаётся короткий дефис, как пишут люди."""
    t = text.strip()
    t = t.replace("\u2014", "-").replace("\u2013", "-")  # — – → -
    t = re.sub(r"```[a-zA-Z]*\s*", "", t)
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"^#+\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^[Тт]ема\s*:?\s*", "", t, flags=re.MULTILINE)
    t = t.strip().strip('"«»').strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _word_count(text):
    return len(re.findall(r"\w+", text))


def _looks_truncated(body):
    """Оборванный ответ: не заканчивается знаком конца предложения.
    Глюк бесплатных прокси — буква бракуется и перегенерируется."""
    tail = body.rstrip().rstrip('"«»').rstrip()
    return bool(tail) and tail[-1] not in ".!?"


_SUBJECT_BANNED = ("инициативн", "знакомств", "предложение о сотрудничестве",
                   "резюме на рассмотрение")


def _subject_ok(subject):
    """Тема должна быть вида «Отклик на вакансию ...» — без «инициативных
    писем», «знакомств» и прочих кривых форматов."""
    low = subject.lower()
    return not any(word in low for word in _SUBJECT_BANNED)


def _split_answer(raw, fallback_subject):
    text = _normalize(raw)
    if not text:
        return "", ""
    parts = text.split("\n", 1)
    subject = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""
    if not subject or len(subject) > 150 or not body:
        # модель ответила не по формату — считаем весь текст телом
        body = text
        subject = fallback_subject
    return subject, body


def _error_brief(exc):
    """Краткая ошибка для лога: тип + сообщение сервера (без ключей)."""
    detail = (str(exc) or "").strip()
    if not detail:
        return type(exc).__name__
    return "{}: {}".format(type(exc).__name__, detail[:120])


def generate_letter(title, company, description, profile_text, attach_resume=False):
    """Возвращает (subject, body) или None, если ни один провайдер не дал годного письма.

    Ключи не попадают ни в промпт, ни в логи: логируется только id провайдера
    и код ошибки. Провайдеры перебираются по кругу, у каждого — до двух
    попыток (исходная + один повтор), кроме фатальных статусов (401/402/403/429),
    после которых провайдер пропускается сразу.
    """
    global _provider_offset
    if attach_resume:
        attachment_line = (
            "резюме приложено отдельным файлом во вложении — упомяни это одной "
            "короткой фразой в конце письма"
        )
    else:
        attachment_line = "никаких упоминаний вложений и прикреплённых файлов"

    if description.strip():
        # режим вакансии: описание заполнено
        prompt = USER_PROMPT_TEMPLATE.format(
            candidate_name=config.CANDIDATE_NAME,
            title=title.strip(),
            company=company.strip(),
            description=description.strip()[:4000],
            profile=profile_text[:config.PROFILE_MAX_CHARS],
            min_words=config.BODY_WORDS_RANGE[0],
            max_words=config.BODY_WORDS_RANGE[1],
            highlights="; ".join(config.HIGHLIGHTS),
            attachment_line=attachment_line,
        )
        fallback_subject = "Отклик на вакансию {}".format(title.strip())
    else:
        # холодный режим: вакансии нет — письмо-знакомство, «продаём» кандидата
        prompt = USER_PROMPT_COLD_TEMPLATE.format(
            candidate_name=config.CANDIDATE_NAME,
            title=title.strip() or "QA-тестирование",
            company=company.strip() or "вашей компании",
            profile=profile_text[:config.PROFILE_MAX_CHARS],
            min_words=config.BODY_WORDS_RANGE[0],
            max_words=config.BODY_WORDS_RANGE[1],
            highlights="; ".join(config.HIGHLIGHTS),
            attachment_line=attachment_line,
        )
        fallback_subject = "Отклик на вакансию {}".format(title.strip() or "QA-тестировщик")

    providers = _load_providers()
    if not providers:
        logging.error("LLM-провайдеры не найдены: нет llm_providers.json и DEEPSEEK_API_KEY")
        return None

    total = len(providers)
    start = _provider_offset % total
    for i in range(total):
        provider = providers[(start + i) % total]
        if _provider_empty_counts.get(provider["id"], 0) >= _EMPTY_SKIP_THRESHOLD:
            logging.info("%s: пропущен - слишком много бракованных ответов в этом запуске",
                         provider["id"])
            continue
        for attempt in (1, 2):
            try:
                raw = _call_provider(provider, prompt)
            except (LLMError, requests.RequestException) as exc:
                logging.warning("%s: попытка %d/2 не удалась (%s)",
                                provider["id"], attempt, _error_brief(exc))
                if getattr(exc, "status_code", None) in _FATAL_STATUSES:
                    break  # лимит/ключ — к следующему провайдеру сразу
                continue  # сетевая или серверная ошибка — один повтор
            subject, body = _split_answer(raw, fallback_subject)
            words = _word_count(body)
            if subject and body and words >= _MIN_BODY_WORDS:
                if _looks_truncated(body):
                    logging.warning(
                        "%s: ответ оборван посреди фразы (попытка %d/2)",
                        provider["id"], attempt,
                    )
                    _provider_empty_counts[provider["id"]] = (
                        _provider_empty_counts.get(provider["id"], 0) + 1
                    )
                    continue  # брак — повтор или следующий провайдер
                _provider_empty_counts[provider["id"]] = 0  # успех сбрасывает счётчик
                if not _subject_ok(subject):
                    logging.warning("%s: тема '%.60s' не подходит — заменяю на запасную",
                                    provider["id"], subject)
                    subject = fallback_subject
                _provider_offset = (start + i + 1) % total  # следующий запрос — соседний провайдер
                return subject, body
            _provider_empty_counts[provider["id"]] = (
                _provider_empty_counts.get(provider["id"], 0) + 1
            )
            logging.warning(
                "%s: пустой или слишком короткий ответ (попытка %d/2, слов в теле: %d)",
                provider["id"], attempt, words,
            )
    logging.error("Ни один из %d LLM-провайдеров не дал годного письма — вакансия будет пропущена", total)
    return None
