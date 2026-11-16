# -*- coding: utf-8 -*-
"""پاک‌سازیِ ردیف‌های یتیم: سشن/پیامی که به مشتریِ حذف‌شده اشاره دارد."""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

DB = Path("data/user_records.db")
LIVE = (1, 2, 3, 4)
ph = ",".join("?" * len(LIVE))

con = sqlite3.connect(str(DB), timeout=15)
sess = [r[0] for r in con.execute(
    f"SELECT token FROM widget_sessions WHERE tenant_id NOT IN ({ph})", LIVE)]
msgs = con.execute(
    f"SELECT COUNT(*) FROM widget_messages WHERE tenant_id NOT IN ({ph})", LIVE).fetchone()[0]
print(f"سشنِ یتیم: {len(sess)}   پیامِ یتیم: {msgs}")
if not sess and not msgs:
    print("چیزی برای پاک‌سازی نیست.")
    con.close()
    raise SystemExit(0)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
bdir = Path("data/backups"); bdir.mkdir(parents=True, exist_ok=True)
dst = bdir / f"{DB.name}.{stamp}"
shutil.copy2(DB, dst)
print(f"پشتیبان: {dst}")

cur = con.cursor()
if sess:
    cur.execute(f"DELETE FROM widget_messages WHERE token IN ({','.join('?' * len(sess))})", sess)
cur.execute(f"DELETE FROM widget_sessions WHERE tenant_id NOT IN ({ph})", LIVE)
cur.execute(f"DELETE FROM widget_messages WHERE tenant_id NOT IN ({ph})", LIVE)
con.commit()
print("کامیت شد.")
print("یتیمِ باقی‌مانده:",
      cur.execute(f"SELECT COUNT(*) FROM widget_sessions WHERE tenant_id NOT IN ({ph})", LIVE).fetchone()[0],
      cur.execute(f"SELECT COUNT(*) FROM widget_messages WHERE tenant_id NOT IN ({ph})", LIVE).fetchone()[0])
print("tenant اصلی دست‌نخورده:",
      cur.execute("SELECT COUNT(*) FROM widget_sessions WHERE tenant_id=1").fetchone()[0], "سشن،",
      cur.execute("SELECT COUNT(*) FROM widget_messages WHERE tenant_id=1").fetchone()[0], "پیام")
con.close()
