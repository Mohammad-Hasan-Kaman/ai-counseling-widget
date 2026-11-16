# -*- coding: utf-8 -*-
"""اعمالِ مالکیتِ نوبت‌ها روی دیتابیسِ زنده: ستون tenant_id + بک‌فیلِ tenant اصلی.

همان کاری که app/crawler.py در شروعِ سرور انجام می‌دهد، اینجا یک‌بار
روی دادهٔ موجود اجرا می‌شود تا tenant اصلی با فیلترِ جدید بی‌نوبت نماند.
"""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

APPT = Path("data/appointments.db")
USR = Path("data/user_records.db")

u = sqlite3.connect(f"file:{USR.as_posix()}?mode=ro", uri=True)
row = u.execute("SELECT id FROM tenants WHERE slug='demo' AND active=1").fetchone()
u.close()
if not row:
    print("❌ tenant اصلی پیدا نشد — لغو شد.")
    raise SystemExit(1)
tid = row[0]
print(f"tenant اصلی: tenant_id={tid}")

con = sqlite3.connect(str(APPT), timeout=15)
cols = [c[1] for c in con.execute("PRAGMA table_info(appointments)")]
print(f"ستون‌های فعلی: {cols}")
total = con.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
bdir = Path("data/backups"); bdir.mkdir(parents=True, exist_ok=True)
dst = bdir / f"{APPT.name}.{stamp}"
shutil.copy2(APPT, dst)
print(f"پشتیبان: {dst}")

cur = con.cursor()
if "tenant_id" not in cols:
    cur.execute("ALTER TABLE appointments ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 0")
    print("ستون tenant_id اضافه شد.")
cur.execute("CREATE INDEX IF NOT EXISTS idx_appt_tenant_status ON appointments(tenant_id, status)")

orphan = cur.execute("SELECT COUNT(*) FROM appointments WHERE tenant_id=0").fetchone()[0]
if orphan:
    cur.execute("UPDATE appointments SET tenant_id=? WHERE tenant_id=0", (tid,))
    print(f"{orphan} نوبتِ بی‌مالک به tenant اصلی نسبت داده شد.")
con.commit()

print(f"\nتوزیعِ مالکیت:")
for r in cur.execute("SELECT tenant_id, status, COUNT(*) c FROM appointments GROUP BY tenant_id, status ORDER BY tenant_id, c DESC"):
    print(f"  tenant={r[0]:<4} {r[1]:<14} {r[2]}")
print(f"\nجمع: {cur.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]} (پیش از این: {total})")
con.close()
