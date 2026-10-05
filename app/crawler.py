# -*- coding: utf-8 -*-
import json
import logging
import sqlite3
import time
import requests
from bs4 import BeautifulSoup
from app.config import APPOINTMENTS_DB, MAPPING_JSON, CRAWLER_DELAY_SECONDS, CRAWLER_HEADERS

log = logging.getLogger(__name__)
TEAM_URL = "https://nikravan.org/team/"

# The system proxy (e.g. a VPN client on port 10808) is sometimes unstable.
# Strategy: go through the system proxy first, fall back to a direct connection (no proxy) on error
DIRECT_PROXIES = {"http": None, "https": None}   # bypass the system proxy
FETCH_RETRIES = 2          # each URL is retried at most 2 times
RETRY_BACKOFF = 3          # seconds to wait between attempts


def fetch_page(url: str) -> str:
    """
    Fetch a page with resilience:
    Attempt 1: default route (system proxy if enabled)
    Attempt 2: after the backoff, a direct connection without a proxy
    """
    last_err = None
    for attempt in range(FETCH_RETRIES):
        proxies = DIRECT_PROXIES if attempt > 0 else None
        try:
            resp = requests.get(url, headers=CRAWLER_HEADERS, timeout=15, proxies=proxies)
            return resp.text
        except Exception as e:
            last_err = e
            if attempt < FETCH_RETRIES - 1:
                log.warning("⚠️ تلاش %d برای %s ناموفق بود؛ %d ثانیه دیگر با مسیر جایگزین...", attempt + 1, url.split('/')[-1], RETRY_BACKOFF)
                time.sleep(RETRY_BACKOFF)
    raise last_err


def _db(sql, params=(), fetch=False):
    conn = sqlite3.connect(str(APPOINTMENTS_DB))
    cur = conn.cursor()
    cur.execute(sql, params)
    result = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result


def init_db():
    _db("""CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        counselor_name TEXT, date TEXT, time TEXT, room TEXT,
        status TEXT, branch TEXT, fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    _db("CREATE INDEX IF NOT EXISTS idx_appt_name_status ON appointments(counselor_name, status)")
    _db("""CREATE TABLE IF NOT EXISTS crawl_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP,
        total INTEGER DEFAULT 0,
        done INTEGER DEFAULT 0,
        free_slots INTEGER DEFAULT 0,
        ok INTEGER DEFAULT 0,
        error TEXT
    )""")
    _db("CREATE INDEX IF NOT EXISTS idx_crawl_runs_started ON crawl_runs(started_at)")


def record_crawl_run(total: int, done: int, free_slots: int, ok: bool, error: str | None = None) -> None:
    """Record the result of every crawl run (automatic or manual) for health monitoring on the dashboard"""
    _db(
        "INSERT INTO crawl_runs (finished_at, total, done, free_slots, ok, error)"
        " VALUES (CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)",
        (total, done, free_slots, 1 if ok else 0, (error or "")[:500]),
    )


def get_last_crawl_info() -> dict | None:
    """Last recorded crawl run: {finished_at, total, done, free_slots, ok, error} or None"""
    rows = _db(
        "SELECT finished_at, total, done, free_slots, ok, error FROM crawl_runs"
        " ORDER BY id DESC LIMIT 1",
        fetch=True,
    )
    if not rows:
        return None
    finished_at, total, done, free_slots, ok, error = rows[0]
    return {
        "finished_at": finished_at, "total": total, "done": done,
        "free_slots": free_slots, "ok": bool(ok), "error": error or "",
    }


def scrape_team_list() -> int:
    try:
        html = fetch_page(TEAM_URL)
        soup = BeautifulSoup(html, "html.parser")

        mapping = {}
        for heading in soup.select("h4 a"):
            href = heading.get("href", "")
            name = heading.get_text(strip=True)
            if name and href and "team/" in href:
                if not href.startswith("http"):
                    href = "https://nikravan.org" + href
                mapping[name] = href

        if mapping:
            with open(MAPPING_JSON, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)
            log.info("📋 %d مشاور از سایت دریافت و در mapping.json ذخیره شد.", len(mapping))
        return len(mapping)
    except Exception as e:
        log.error("❌ خطای استخراج لیست مشاوران: %s", e)
        return 0


def crawl_available_slots(progress_cb=None):
    """progress_cb(done, total, current_name, free_total) — for live display in the panel"""
    scrape_team_list()
    init_db()
    try:
        with open(MAPPING_JSON, "r", encoding="utf-8") as f:
            mapping = json.load(f)
    except FileNotFoundError:
        log.error("mapping.json یافت نشد.")
        record_crawl_run(0, 0, 0, ok=False, error="mapping.json یافت نشد")
        return

    total = len(mapping)
    log.info("🔍 در حال استخراج نوبت‌های %d مشاور...", total)

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 5   # if the network is completely down, the whole crawl stops after 5 consecutive errors
    done = 0
    free_total = 0
    stopped_early = False
    last_error: str | None = None

    def _report(current: str = ""):
        if progress_cb:
            try:
                progress_cb(done, total, current, free_total)
            except Exception:
                pass

    _report("شروع...")

    for name, url in mapping.items():
        _report(name)
        try:
            html = fetch_page(url)
        except Exception as e:
            # network/proxy error: this counselor's previous data is left untouched
            consecutive_errors += 1
            last_error = str(e)[:200]
            log.error("❌ خطای اتصال هنگام کراول %s: %s", name, e)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error("🛑 اتصال به سایت برقرار نیست؛ کراول متوقف شد و داده قبلی حفظ ماند.")
                stopped_early = True
                _report("توقف به‌علت قطعی اتصال")
                break
            done += 1
            _report(name)
            time.sleep(CRAWLER_DELAY_SECONDS)
            continue

        consecutive_errors = 0

        try:
            soup = BeautifulSoup(html, "html.parser")

            # Priority 1: the appointments table — the "waiting list"/"phone call" phrases may also appear in
            # the menu or footer of any page, so the table must be checked first.
            table = soup.find("table", {"id": "report"}) or soup.find("table", class_="table")
            free_slots = []
            if table:
                for row in (table.find("tbody") or table).find_all("tr"):
                    if row.get("data-status") != "free":
                        continue
                    cols = row.find_all("td")
                    if len(cols) < 3:
                        continue
                    link = cols[2].find("a", href=True)
                    if link and "schedules_edit" in link["href"]:
                        date = cols[0].get_text(strip=True)
                        span = cols[1].find("span")
                        time_slot = span.get_text(strip=True) if span else ""
                        room_el = cols[1].find("small")
                        room_txt = room_el.get_text(strip=True) if room_el else ""
                        branch = row.get("data-branch", "")
                        # infer the branch from the room text when data-branch is empty
                        if not branch and room_txt:
                            if "ظفر" in room_txt or "زعفرانیه" in room_txt:
                                branch = "zafar"
                            elif "ایران" in room_txt:
                                branch = "iran"
                        free_slots.append((date, time_slot, room_txt, branch))

            if free_slots:
                _db("DELETE FROM appointments WHERE counselor_name=?", (name,))
                for date, time_slot, room_txt, branch in free_slots:
                    _db(
                        "INSERT INTO appointments (counselor_name,date,time,room,status,branch) VALUES (?,?,?,?,?,?)",
                        (name, date, time_slot, room_txt, "free", branch),
                    )
                free_total += len(free_slots)
                log.info("🟢 %s: %d نوبت آزاد ثبت شد.", name, len(free_slots))
                done += 1
                time.sleep(CRAWLER_DELAY_SECONDS)
                continue

            # Priority 2: status messages — only valid when the table has no free slots
            page_text = soup.get_text()
            _db("DELETE FROM appointments WHERE counselor_name=?", (name,))

            if "هم اکنون نوبت آزاد ندارد" in page_text:
                _db("INSERT INTO appointments (counselor_name,status) VALUES (?,?)", (name, "no_available"))
            elif "تماس تلفنی" in page_text or "صرفا با تماس" in page_text:
                _db("INSERT INTO appointments (counselor_name,status) VALUES (?,?)", (name, "phone_only"))
            elif "لیست انتظار" in page_text or table is not None:
                # a page with a table but no free rows = everything is booked; the word "waiting list" appearing only in the menu also means waiting
                _db("INSERT INTO appointments (counselor_name,status) VALUES (?,?)", (name, "waiting"))
            else:
                _db("INSERT INTO appointments (counselor_name,status) VALUES (?,?)", (name, "no_table"))

        except Exception as e:
            log.error("❌ خطای پردازش صفحه %s: %s", name, e)

        done += 1
        time.sleep(CRAWLER_DELAY_SECONDS)

    _report("تکمیل شد")
    if stopped_early:
        record_crawl_run(total, done, free_total, ok=False, error=last_error or "توقف به‌علت قطعی اتصال")
        log.warning("⚠️ کراول ناقص به پایان رسید: %d از %d مشاور.", done, total)
    else:
        record_crawl_run(total, done, free_total, ok=True)
    log.info("✅ فرآیند استخراج نوبت‌ها تکمیل شد.")


if __name__ == "__main__":
    crawl_available_slots()