# -*- coding: utf-8 -*-
"""بررسیِ HTTP روی کپی از دیتابیسِ زنده، داخل یک thread پس‌زمینه.

همان سناریویی که مرورگر می‌دید: لاگین، داشبورد، ویجت.
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import http.cookiejar
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="nk_http_"))
for n in ("user_records.db", "appointments.db", "ai_knowledge.db"):
    src = ROOT / "data" / n
    if src.exists():
        shutil.copy2(src, TMP / n)
os.environ.update({
    "USER_RECORDS_DB": str(TMP / "user_records.db"),
    "APPOINTMENTS_DB": str(TMP / "appointments.db"),
    "AI_KNOWLEDGE_DB": str(TMP / "ai_knowledge.db"),
    "PROFILES_JSON": str(TMP / "missing.json"),
    "MAPPING_JSON": str(TMP / "missing.json"),
    "CRAWLER_ENABLED": "0",
})

# رمزها از .env واقعی خوانده می‌شوند — رمزِ ساختگی با هشِ ذخیره‌شده نمی‌خواند
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line.startswith("ADMIN_PANEL_PASSWORD=") or line.startswith("SUPER_ADMIN_PASSWORD="):
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip().strip('"').strip("'")
REAL_ADMIN_PW = os.environ["ADMIN_PANEL_PASSWORD"]

import logging  # noqa: E402
import uvicorn  # noqa: E402
logging.disable(logging.CRITICAL)   # لاگ‌های APScheduler خروجی را می‌شکنند
from app.main import app  # noqa: E402
from app.db import init_all_dbs  # noqa: E402

init_all_dbs()
PORT = 8765
srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="critical"))
threading.Thread(target=srv.run, daemon=True).start()

BASE = f"http://127.0.0.1:{PORT}"
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/pricing", timeout=1).read()
        break
    except Exception:
        time.sleep(0.25)

jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
REPORT: list[str] = []


def say(*a):
    REPORT.append(" ".join(str(x) for x in a))


def get(path):
    r = op.open(BASE + path, timeout=10)
    return r.status, r.read().decode("utf-8", "replace"), r.geturl()


def post(path, data, headers=None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=headers or {})
    try:
        r = op.open(req, timeout=10)
        return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


say("=" * 60)
for p in ("/pricing", "/", "/health"):
    try:
        s, b, _ = get(p)
        say(f"  {p:<14} {s}  ({len(b)} بایت)")
    except Exception as e:
        say(f"  {p:<14} خطا: {e}")

say("\n--- صفحهٔ قیمت: اعدادِ فروخته‌شده ---")
_, body, _ = get("/pricing")
for probe in ("۳۰", "۵۰۰", "۱٬۵۰۰", "نامحدود"):
    say(f"  «{probe}» در صفحه: {'هست' if probe in body else 'نیست'}")

say("\n--- لاگین به‌عنوان ادمین مرکز نمونه ---")
s, _, h = post("/admin/login",
               {"username": "demo", "password": REAL_ADMIN_PW},
               {"Origin": BASE})
say(f"  وضعیت: {s}  -> {h.get('Location', '')}")
say(f"  کوکی: {[c.name for c in jar]}")

say("\n--- داشبوردِ مرکز نمونه ---")
s, body, url = get("/admin")
say(f"  وضعیت: {s}  مسیر: {url}")
import re
text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
for label in ("نوبت آزاد", "معرفی", "پیام", "سقف"):
    for m in re.finditer(label, text):
        say(f"  «{label}»: …{text[max(0, m.start() - 90):m.start() + 90].strip()}…")
        break

say("\n--- گزارش دوره‌ای (باید روی پلنِ پایه رد شود) ---")
s, b, u2 = get("/admin/report")
say(f"  وضعیت: {s}  مقصد: {u2}")
t2 = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b))
say(f"  پیام: …{t2[:220]}…")

srv.should_exit = True
say(f"\nکپی در: {TMP}")
(ROOT / "tools" / "_http_result.txt").write_text("\n".join(REPORT), encoding="utf-8")
