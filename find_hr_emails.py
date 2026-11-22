"""Автопоиск HR-email на сайтах компаний из sites_qa.csv.

- компании, у которых email уже найден ранее (опциональный файл contacts.csv рядом),
  берутся сразу, без сети;
- по остальным: главная страница + до 5 внутренних ссылок на контакты/карьеру;
- email фильтруются от мусора (картинки, noreply, cloudflare и т.п.), лучшая
  адресация — с hr/jobs/career в локальной части;
- результат: emails_found.csv (company, site, best_email, all_emails, source).

Запуск: python find_hr_emails.py  (долгий, выводит прогресс)
"""

import csv
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = Path(__file__).resolve().parent
SITES_CSV = BASE / "sites_qa.csv"
PILOT_CSV = BASE.parent / "outreach" / "contacts.csv"
OUT_CSV = BASE / "emails_found.csv"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
BAD_TAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js|ico|woff2?)$", re.I)
BAD_LOCAL = re.compile(
    r"^(noreply|no-reply|donotreply|abuse|postmaster|webmaster|hostmaster|"
    r"cloudflare|wordpress|example|test|user|info@)?[0-9a-f]{16,}$", re.I)
BAD_LOCAL2 = re.compile(
    r"^(noreply|no-reply|donotreply|abuse|postmaster|webmaster|hostmaster|"
    r"cloudflare|wordpress|example|test)@", re.I)
GOOD_HINT = re.compile(r"(hr|jobs?|career|recruit|talent|кадр|персонал|vacanc)", re.I)
NEUTRAL_HINT = re.compile(r"(info|office|contact|hello|mail)", re.I)
LINK_RE = re.compile(r'href=["\']([^"\'#?]+)', re.I)
KEY_LINK = re.compile(r"(контакт|contact|о-компани|о компании|about|career|"
                      r"вакан|vacan|jobs|работ[ау]|join)", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/125.0 Safari/537.36"}


def norm_domain(url):
    host = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def make_session(trust_env):
    s = requests.Session()
    s.trust_env = trust_env
    s.headers.update(HEADERS)
    return s


def fetch(session, url):
    for verify in (True, False):
        try:
            r = session.get(url, timeout=10, verify=verify)
            if r.status_code == 200 and "text" in r.headers.get("Content-Type", "text"):
                return r.text
            return ""
        except requests.RequestException:
            continue
    return ""


def clean_emails(html):
    out = set()
    for raw in EMAIL_RE.findall(html):
        e = raw.strip(".").strip().lower()
        if BAD_TAIL.search(e):
            continue
        if BAD_LOCAL2.match(e):
            continue
        if BAD_LOCAL.match(e):
            continue
        if len(e) > 60:
            continue
        out.add(e)
    return out


def rank(emails, domain):
    def score(e):
        local, _, edom = e.partition("@")
        s = 0
        if GOOD_HINT.search(local):
            s -= 3
        if edom == domain or edom.endswith("." + domain):
            s -= 1
        if NEUTRAL_HINT.search(local):
            s -= 1
        if re.search(r"(support|help|admin|sales|shop|order|zakaz|billing)", local):
            s += 2
        return s
    return sorted(emails, key=score)[0] if emails else ""


def hunt(site):
    """Возвращает (emails_set, pages_fetched)."""
    base = site if site.startswith("http") else "https://" + site
    domain = norm_domain(site)
    emails, pages = set(), 0
    session = make_session(trust_env=False)
    home = fetch(session, base)
    pages += 1
    if not home:
        session = make_session(trust_env=True)  # запасной путь через прокси
        home = fetch(session, base)
        pages += 1
    if not home:
        return emails, pages

    emails |= clean_emails(home)
    if emails:
        return emails, pages

    links, seen = [], set()
    for href in LINK_RE.findall(home):
        if not KEY_LINK.search(href):
            continue
        full = urljoin(base, href)
        if norm_domain(full) != domain:
            continue
        if full.rstrip("/") == base.rstrip("/"):
            continue
        if full not in seen:
            seen.add(full)
            links.append(full)
        if len(links) >= 5:
            break
    for link in links:
        found = clean_emails(fetch(session, link))
        pages += 1
        emails |= found
        if emails:
            break
    return emails, pages


def pilot_emails():
    """company_key и domain -> email из опционального пилотного файла contacts.csv."""
    by_key = {}
    try:
        for r in csv.DictReader(open(PILOT_CSV, encoding="utf-8-sig")):
            emails = set()
            if (r.get("contact_type") or "").strip().lower() == "email":
                contact = (r.get("contact") or "").strip()
                if contact and "@" in contact:
                    emails.add(contact.lower())
            for e in (r.get("emails_all") or "").split(";"):
                e = e.strip().lower()
                if "@" in e:
                    emails.add(e)
            if not emails:
                continue
            best = sorted(emails, key=lambda e: (not GOOD_HINT.search(e), e))[0]
            name = (r.get("company") or "").strip().lower()
            if name:
                by_key.setdefault("name:" + name, (best, "; ".join(sorted(emails))))
            site = (r.get("site") or "").strip()
            if site:
                by_key.setdefault("domain:" + norm_domain(site),
                                  (best, "; ".join(sorted(emails))))
    except OSError:
        pass
    return by_key


def main():
    companies = list(csv.DictReader(open(SITES_CSV, encoding="utf-8-sig")))
    known = pilot_emails()
    print("Компаний в списке:", len(companies), "| email из пилота:", len(known))

    results = []
    for i, row in enumerate(companies, 1):
        company = row["company"].strip()
        site = row["site"].strip()
        domain = norm_domain(site)

        pre = known.get("name:" + company.lower()) or known.get("domain:" + domain)
        if pre:
            results.append([company, site, pre[0], pre[1], "контакты", 0])
            print("[{}/{}] {} -> {} (пилот)".format(i, len(companies), company, pre[0]))
            continue

        try:
            emails, pages = hunt(site)
        except Exception as exc:  # сетевые сюрпризы не роняют обход
            results.append([company, site, "", "", "ошибка: " + type(exc).__name__, 0])
            print("[{}/{}] {} -> ошибка {}".format(i, len(companies), company,
                                                   type(exc).__name__))
            continue
        best = rank(emails, domain)
        source = "поиск" if best else "не найдено"
        results.append([company, site, best, "; ".join(sorted(emails)), source, pages])
        print("[{}/{}] {} -> {} (страниц: {})".format(i, len(companies), company,
                                                      best or "не найдено", pages))
        time.sleep(0.2)

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "site", "best_email", "all_emails", "source", "pages"])
        w.writerows(results)

    ok = sum(1 for r in results if r[2])
    print()
    print("Готово: {} компаний, с email: {}, файл: {}".format(len(results), ok, OUT_CSV))


if __name__ == "__main__":
    main()
