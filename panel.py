"""Локальная веб-панель управления outreach-mailer.

Запуск: двойной клик по настройкам_бота.bat (или: python panel.py).
Браузер откроется сам на http://127.0.0.1:8766. Сервер слушает ТОЛЬКО localhost.

Панель правит: settings.json (настройки), vacancies.json (компании), .env (пароли).
Секреты не возвращаются в ответах API и не логируются - только признак «задано/пусто».
Настройки применяются ботом при следующем запуске (запуск_бота.bat).
Только стандартная библиотека, никаких новых зависимостей.
"""

import csv
import json
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"
VACANCIES_FILE = BASE_DIR / "vacancies.json"
SENT_LOG_FILE = BASE_DIR / "sent_log.csv"
ENV_FILE = BASE_DIR / ".env"
LOCK_FILE = BASE_DIR / "bot.lock"
LOG_FILE = BASE_DIR / "logs" / "app.log"
MAIN_PY = BASE_DIR / "main.py"
PORT = 8766

_DEFAULT_SETTINGS = {
    "max_per_run": 30,
    "pause_min": 20,
    "pause_max": 60,
    "attach_resume": True,
    "send_copy_to_self": True,
    "allow_resend": False,
}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def validate_settings(raw):
    """(clean, errors): проверка значений формы. Никогда не бросает исключений."""
    errors = []

    def as_int(key, lo, hi, default):
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            errors.append("{}: должно быть числом".format(key))
            return default
        if not lo <= value <= hi:
            errors.append("{}: должно быть от {} до {}".format(key, lo, hi))
            return default
        return value

    max_per_run = as_int("max_per_run", 1, 200, _DEFAULT_SETTINGS["max_per_run"])
    pause_min = as_int("pause_min", 5, 600, _DEFAULT_SETTINGS["pause_min"])
    pause_max = as_int("pause_max", 5, 600, _DEFAULT_SETTINGS["pause_max"])
    if pause_min > pause_max:
        errors.append("Пауза: минимум больше максимума - поменял местами")
        pause_min, pause_max = pause_max, pause_min

    clean = {
        "max_per_run": max_per_run,
        "pause_min": pause_min,
        "pause_max": pause_max,
        "attach_resume": bool(raw.get("attach_resume", True)),
        "send_copy_to_self": bool(raw.get("send_copy_to_self", True)),
        "allow_resend": bool(raw.get("allow_resend", False)),
    }
    if clean["allow_resend"]:
        errors.append(
            "ВНИМАНИЕ: включён ALLOW_RESEND - блок повторных отправок отключён. "
            "Это только для тестов на СОБСТВЕННЫХ адресах!"
        )
    return clean, errors


def load_settings():
    return validate_settings(_read_json(SETTINGS_FILE, {}))[0]


def save_settings(clean):
    SETTINGS_FILE.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def validate_vacancies(rows):
    """(clean, errors): убирает битые записи и дубли адресов."""
    clean, errors, seen = [], [], set()
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append("Запись {}: не объект".format(i))
            continue
        title = (row.get("title") or "").strip()
        company = (row.get("company") or "").strip()
        description = (row.get("description") or "").strip()
        email = (row.get("hr_email") or "").strip()
        if not title:
            errors.append("Запись {}: пустое название вакансии".format(i))
            continue
        if not email or not _EMAIL_RE.match(email):
            errors.append("Запись {}: некорректный hr_email".format(i))
            continue
        key = email.lower()
        if key in seen:
            errors.append("Запись {}: дубликат адреса {} - пропущена".format(i, email))
            continue
        seen.add(key)
        clean.append({
            "title": title,
            "company": company,
            "description": description,
            "hr_email": email,
        })
    return clean, errors


def load_vacancies():
    data = _read_json(VACANCIES_FILE, [])
    return data if isinstance(data, list) else []


def save_vacancies(clean):
    VACANCIES_FILE.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def env_status():
    """Какие секреты заданы в .env (только да/нет, без значений)."""
    result = {"mailru_set": False, "deepseek_set": False}
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                key = key.strip()
                if key == "MAILRU_APP_PASSWORD" and value.strip():
                    result["mailru_set"] = True
                if key == "DEEPSEEK_API_KEY" and value.strip():
                    result["deepseek_set"] = True
        except OSError:
            pass
    return result


def update_env(updates):
    """Обновляет ТОЛЬКО непустые значения из формы. Пустое поле = не менять.
    Значения никуда не выводятся."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    for key in ("MAILRU_APP_PASSWORD", "DEEPSEEK_API_KEY"):
        value = (updates.get(key) or "").strip()
        if not value:
            continue
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith(key + "="):
                lines[i] = "{}={}".format(key, value)
                replaced = True
                break
        if not replaced:
            lines.append("{}={}".format(key, value))
    if lines:
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_status()


def collect_stats():
    counts = {"total": 0, "sent": 0, "skipped": 0, "error": 0}
    last = []
    if SENT_LOG_FILE.exists():
        try:
            with SENT_LOG_FILE.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    counts["total"] += 1
                    status = (row.get("status") or "").strip().lower()
                    if status in counts:
                        counts[status] += 1
                    last.append(row)
        except OSError:
            pass
    return {"counts": counts, "last": last[-10:]}


def state():
    return {
        "settings": load_settings(),
        "vacancies": load_vacancies(),
        "env": env_status(),
        "stats": collect_stats(),
        "bot_running": bot_running(),
        "note": "Настройки применяются при следующем запуске бота (запуск_бота.bat)",
    }


# --- запуск бота прямо из панели ---

def _pid_alive(pid_text):
    """Жив ли процесс с таким PID (Windows)."""
    import ctypes

    try:
        pid = int(pid_text)
    except (TypeError, ValueError):
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def bot_running():
    """Бот считается работающим, пока жив bot.lock с его PID."""
    if not LOCK_FILE.exists():
        return False
    alive = _pid_alive(LOCK_FILE.read_text(encoding="utf-8").strip())
    if not alive:
        try:
            LOCK_FILE.unlink(missing_ok=True)  # мёртвый хвост от краша
        except OSError:
            pass
    return alive


def run_bot():
    """Стартует main.py отдельным процессом. Дубль не допускается."""
    if bot_running():
        return {"ok": False, "errors": ["Бот уже работает — сначала дождись окончания"]}
    log_dir = LOG_FILE.parent
    log_dir.mkdir(exist_ok=True)
    console = open(log_dir / "run_console.log", "ab")
    subprocess.Popen(  # отсоединённый процесс: панель можно закрыть
        [sys.executable, "-u", str(MAIN_PY)],
        cwd=str(BASE_DIR),
        stdout=console,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    )
    return {"ok": True, "errors": [], "note": "Бот запущен — прогресс в логе ниже"}


def logtail(lines=40):
    tail = []
    if LOG_FILE.exists():
        try:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            tail = []
    return {"running": bot_running(), "tail": tail}


def apply_save(payload):
    errors = []
    settings, s_errors = validate_settings(payload.get("settings") or {})
    errors.extend(s_errors)
    vacancies, v_errors = validate_vacancies(payload.get("vacancies") or [])
    errors.extend(v_errors)

    env_updates = payload.get("env") or {}
    if not isinstance(env_updates, dict):
        env_updates = {}

    if errors:
        return {"ok": False, "errors": errors, "state": state()}

    save_settings(settings)
    save_vacancies(vacancies)
    env_after = update_env(env_updates) if env_updates else env_status()
    return {"ok": True, "errors": [], "env": env_after, "state": state()}


PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Outreach Mailer - панель управления</title>
<style>
  body { font-family: Arial, sans-serif; margin: 0; background: #f4f6f8; color: #222; }
  .wrap { max-width: 960px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 22px; margin: 12px 0; }
  h2 { font-size: 17px; margin: 20px 0 8px; border-bottom: 2px solid #2b6cb0; padding-bottom: 4px; }
  .card { background: #fff; border: 1px solid #d5dbe2; border-radius: 8px; padding: 14px; margin-bottom: 14px; }
  label { display: block; margin: 8px 0 2px; font-weight: bold; font-size: 14px; }
  input[type=text], input[type=number], input[type=password], textarea, select {
      width: 100%; box-sizing: border-box; padding: 6px 8px; border: 1px solid #b9c2cc;
      border-radius: 4px; font-size: 14px; font-family: inherit; }
  textarea { min-height: 54px; resize: vertical; }
  .row { margin-bottom: 10px; }
  .checkbox { font-weight: normal; font-size: 14px; display: flex; gap: 8px; align-items: center; margin: 10px 0; }
  button { padding: 8px 16px; font-size: 14px; border: none; border-radius: 4px; cursor: pointer;
           background: #2b6cb0; color: #fff; }
  button.gray { background: #6b7a8d; }
  button.danger { background: #c0392b; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { border: 1px solid #d5dbe2; padding: 5px 6px; text-align: left; vertical-align: top; }
  th { background: #eef2f6; }
  .msg { padding: 10px; border-radius: 4px; margin: 10px 0; font-size: 14px; display: none; }
  .msg.ok { background: #e2f2e2; border: 1px solid #67a86b; display: block; }
  .msg.err { background: #fbe4e2; border: 1px solid #d9534f; display: block; white-space: pre-wrap; }
  .warn { color: #c0392b; font-weight: bold; }
  .hint { color: #667; font-size: 12px; margin: 4px 0; }
  .del { padding: 2px 8px; font-size: 12px; }
  .stats span { display: inline-block; background: #eef2f6; border-radius: 4px; padding: 4px 10px; margin: 2px 6px 2px 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Outreach Mailer - панель управления</h1>
  <div class="hint">Панель правит settings.json, vacancies.json и .env. Настройки применяются при
  следующем запуске бота (запуск_бота.bat). Сервер работает только на этом компьютере.</div>
  <div id="msg" class="msg"></div>

  <h2>Настройки</h2>
  <div class="card">
    <div class="row">
      <label>Максимум писем за один запуск (1-200)</label>
      <input type="number" id="max_per_run" min="1" max="200">
    </div>
    <div class="row">
      <label>Пауза между письмами, сек - минимум (5-600)</label>
      <input type="number" id="pause_min" min="5" max="600">
    </div>
    <div class="row">
      <label>Пауза между письмами, сек - максимум (5-600)</label>
      <input type="number" id="pause_max" min="5" max="600">
    </div>
    <label class="checkbox"><input type="checkbox" id="attach_resume"> Прикреплять резюме-PDF к письмам</label>
    <label class="checkbox"><input type="checkbox" id="send_copy_to_self"> Отправлять копию каждого письма себе
      (архив отправленного во «Входящих» mail.ru; работодатель копию не видит)</label>
    <label class="checkbox"><input type="checkbox" id="allow_resend"> Разрешить повторную отправку на адрес
      <span class="warn">(ТОЛЬКО для тестов на своих адресах! Для компаний держать ВЫКЛ)</span></label>
    <div class="hint">SMTP-сервер сам не сохраняет письма в «Отправленные» - поэтому копия себе приходит
      во «Входящие». В mail.ru можно создать папку «Копии бота» и фильтр «Кому содержит
      &lt;твой адрес&gt;», чтобы складывать их туда. Резюме-PDF положи рядом с main.py
      (в репозиторий не публикуется). Ключи LLM лежат в llm_providers.json
      (в репозиторий не публикуются).</div>
  </div>

  <h2>Компании и адреса (vacancies.json)</h2>
  <div class="card">
    <table id="vac_table">
      <thead><tr>
        <th style="width:24%">Вакансия (title)</th>
        <th style="width:20%">Компания</th>
        <th style="width:32%">Описание (пусто = отклик на QA-направление)</th>
        <th style="width:18%">Email (hr_email)</th>
        <th style="width:6%"></th>
      </tr></thead>
      <tbody id="vac_body"></tbody>
    </table>
    <p><button class="gray" onclick="addRow()">+ Добавить компанию</button></p>
    <div class="hint">Один адрес = одно письмо навсегда (журнал sent_log.csv). Дубликаты при сохранении
      отбрасываются автоматически.</div>
  </div>

  <h2>Пароли (.env)</h2>
  <div class="card">
    <div class="row">
      <label>Пароль приложения Mail.ru <span id="mailru_status"></span></label>
      <input type="password" id="MAILRU_APP_PASSWORD" placeholder="Оставь пустым, чтобы не менять">
    </div>
    <div class="row">
      <label>Ключ DeepSeek (официальный API, необязательно) <span id="deepseek_status"></span></label>
      <input type="password" id="DEEPSEEK_API_KEY" placeholder="Оставь пустым, чтобы не менять">
    </div>
    <div class="hint">Пароль не показывается и не возвращается из панели - только пометка «задан».
      Ключи бесплатных провайдеров живут отдельно в llm_providers.json.</div>
  </div>

  <h2>Запуск бота</h2>
  <div class="card">
    <p><button id="run_btn" onclick="runBot()">ЗАПУСТИТЬ БОТА</button>
       <span id="bot_status"></span></p>
    <div class="hint">Бот отправит письма по списку выше (лимит из настроек). Копии придут
      во «Входящие» mail.ru. Не закрывай это окно, пока бот не отработает.</div>
    <pre id="logtail" style="max-height:260px; overflow:auto; background:#1e2430; color:#cdd6e0;
         padding:10px; border-radius:6px; font-size:12px;">лог появится здесь</pre>
  </div>

  <h2>Статистика отправок</h2>
  <div class="card">
    <div class="stats" id="stats_counts"></div>
    <div id="stats_last"></div>
  </div>

  <p><button onclick="save()">СОХРАНИТЬ ВСЁ</button> <span id="save_busy"></span></p>
</div>

<script>
function showMsg(ok, text) {
  var box = document.getElementById("msg");
  box.className = "msg " + (ok ? "ok" : "err");
  box.textContent = text;
  window.scrollTo(0, 0);
}

function fillSettings(s) {
  document.getElementById("max_per_run").value = s.max_per_run;
  document.getElementById("pause_min").value = s.pause_min;
  document.getElementById("pause_max").value = s.pause_max;
  document.getElementById("attach_resume").checked = s.attach_resume;
  document.getElementById("send_copy_to_self").checked = s.send_copy_to_self;
  document.getElementById("allow_resend").checked = s.allow_resend;
}

function addRow(v) {
  v = v || {title: "", company: "", description: "", hr_email: ""};
  var tbody = document.getElementById("vac_body");
  var tr = document.createElement("tr");
  function cell(name, value, isArea) {
    var td = document.createElement("td");
    var input;
    if (isArea) {
      input = document.createElement("textarea");
    } else {
      input = document.createElement("input");
      input.type = "text";
    }
    input.dataset.field = name;
    input.value = value || "";
    td.appendChild(input);
    return td;
  }
  tr.appendChild(cell("title", v.title));
  tr.appendChild(cell("company", v.company));
  tr.appendChild(cell("description", v.description, true));
  tr.appendChild(cell("hr_email", v.hr_email));
  var tdDel = document.createElement("td");
  var btn = document.createElement("button");
  btn.className = "danger del";
  btn.textContent = "X";
  btn.onclick = function () { tr.remove(); };
  tdDel.appendChild(btn);
  tr.appendChild(tdDel);
  tbody.appendChild(tr);
}

function fillVacancies(list) {
  document.getElementById("vac_body").innerHTML = "";
  list.forEach(function (v) { addRow(v); });
}

function collectVacancies() {
  var rows = [];
  document.querySelectorAll("#vac_body tr").forEach(function (tr) {
    var row = {};
    tr.querySelectorAll("[data-field]").forEach(function (input) {
      row[input.dataset.field] = input.value;
    });
    rows.push(row);
  });
  return rows;
}

function fillEnv(env) {
  document.getElementById("mailru_status").textContent =
      env.mailru_set ? "(задан)" : "(НЕ задан!)";
  document.getElementById("deepseek_status").textContent =
      env.deepseek_set ? "(задан)" : "(не задан - необязательно, есть llm_providers.json)";
  document.getElementById("MAILRU_APP_PASSWORD").value = "";
  document.getElementById("DEEPSEEK_API_KEY").value = "";
}

function fillStats(stats) {
  var c = stats.counts;
  document.getElementById("stats_counts").innerHTML =
      "<span>Всего записей: " + c.total + "</span>" +
      "<span>Отправлено: " + c.sent + "</span>" +
      "<span>Пропущено: " + c.skipped + "</span>" +
      "<span>Ошибок: " + c.error + "</span>";
  var html = "";
  if (stats.last.length) {
    html = "<table><tr><th>Дата</th><th>Компания</th><th>Вакансия</th><th>Кому</th><th>Статус</th></tr>";
    stats.last.forEach(function (r) {
      html += "<tr><td>" + (r.datetime || "") + "</td><td>" + (r.company || "") + "</td><td>" +
              (r.vacancy || "") + "</td><td>" + (r.to_email || "") + "</td><td>" +
              (r.status || "") + "</td></tr>";
    });
    html += "</table>";
  } else {
    html = "<p>Пока ничего не отправлялось.</p>";
  }
  document.getElementById("stats_last").innerHTML = html;
}

function loadState() {
  fetch("/api/state").then(function (r) { return r.json(); }).then(function (state) {
    fillSettings(state.settings);
    fillVacancies(state.vacancies);
    fillEnv(state.env);
    fillStats(state.stats);
  }).catch(function (e) { showMsg(false, "Не удалось загрузить состояние: " + e); });
}

function save() {
  document.getElementById("save_busy").textContent = "Сохраняю...";
  var payload = {
    settings: {
      max_per_run: document.getElementById("max_per_run").value,
      pause_min: document.getElementById("pause_min").value,
      pause_max: document.getElementById("pause_max").value,
      attach_resume: document.getElementById("attach_resume").checked,
      send_copy_to_self: document.getElementById("send_copy_to_self").checked,
      allow_resend: document.getElementById("allow_resend").checked
    },
    vacancies: collectVacancies(),
    env: {}
  };
  var mailru = document.getElementById("MAILRU_APP_PASSWORD").value.trim();
  var deepseek = document.getElementById("DEEPSEEK_API_KEY").value.trim();
  if (mailru) { payload.env.MAILRU_APP_PASSWORD = mailru; }
  if (deepseek) { payload.env.DEEPSEEK_API_KEY = deepseek; }

  fetch("/api/save", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  }).then(function (r) { return r.json(); }).then(function (result) {
    document.getElementById("save_busy").textContent = "";
    showMsg(result.ok, result.ok ? "Сохранено. Применится при следующем запуске бота." :
            "Ошибки сохранения:\n" + result.errors.join("\n"));
    if (result.state) {
      fillEnv(result.state.env);
      fillStats(result.state.stats);
    }
  }).catch(function (e) {
    document.getElementById("save_busy").textContent = "";
    showMsg(false, "Ошибка запроса: " + e);
  });
}

function runBot() {
  var btn = document.getElementById("run_btn");
  btn.disabled = true;
  fetch("/api/run_bot", {method: "POST"}).then(function (r) { return r.json(); }).then(function (res) {
    btn.disabled = false;
    showMsg(res.ok, res.ok ? (res.note || "Бот запущен") : "Ошибки:\n" + res.errors.join("\n"));
    loadLogtail();
  }).catch(function (e) {
    btn.disabled = false;
    showMsg(false, "Ошибка запуска: " + e);
  });
}

function loadLogtail() {
  fetch("/api/logtail").then(function (r) { return r.json(); }).then(function (res) {
    var box = document.getElementById("logtail");
    box.textContent = res.tail.join("\n") || "лог пуст";
    box.scrollTop = box.scrollHeight;
    var badge = document.getElementById("bot_status");
    badge.textContent = res.running ? "✅ БОТ РАБОТАЕТ" : "⛔ бот не запущен";
    badge.className = res.running ? "hint" : "warn";
    var btn = document.getElementById("run_btn");
    btn.disabled = res.running;
  }).catch(function () {});
}
setInterval(loadLogtail, 3000);
loadLogtail();

loadState();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/api/state":
            self._send(200, json.dumps(state(), ensure_ascii=False), "application/json")
        elif self.path == "/api/logtail":
            self._send(200, json.dumps(logtail(), ensure_ascii=False), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/run_bot":
            self._send(200, json.dumps(run_bot(), ensure_ascii=False), "application/json")
            return
        if self.path != "/api/save":
            self._send(404, "not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, json.dumps({"ok": False, "errors": ["битый JSON"]},
                                       ensure_ascii=False), "application/json")
            return
        result = apply_save(payload)
        code = 200 if result["ok"] else 400
        self._send(code, json.dumps(result, ensure_ascii=False), "application/json")

    def log_message(self, fmt, *args):
        pass  # запросы не логируем: тишина и без секретов


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Панель управления: http://127.0.0.1:{}".format(PORT))
    print("Окно держи открытым. Выход: закрыть окно или Ctrl+C.")
    threading.Timer(1.0, lambda: webbrowser.open(
        "http://127.0.0.1:{}".format(PORT))).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
