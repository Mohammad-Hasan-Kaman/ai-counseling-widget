# -*- coding: utf-8 -*-
"""راه‌اندازیِ کاملِ اپ روی *کپی* از دیتابیسِ زنده — هیچ نوشتنی روی دادهٔ واقعی نیست.

نشان می‌دهد init_all_dbs (بک‌فیلِ نوبت‌ها، migrationها، seed) بدون خطا اجرا
می‌شود و اعدادِ بعدش همان چیزی است که انتظار داریم.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="nk_boot_"))
shutil.copy2(ROOT / "data" / "user_records.db", TMP / "user_records.db")
if (ROOT / "data" / "appointments.db").exists():
    shutil.copy2(ROOT / "data" / "appointments.db", TMP / "appointments.db")
shutil.copy2(ROOT / "data" / "ai_knowledge.db", TMP / "ai_knowledge.db") \
    if (ROOT / "data" / "ai_knowledge.db").exists() else None

os.environ.update({
    "USER_RECORDS_DB": str(TMP / "user_records.db"),
    "APPOINTMENTS_DB": str(TMP / "appointments.db"),
    "AI_KNOWLEDGE_DB": str(TMP / "ai_knowledge.db"),
    "PROFILES_JSON": str(TMP / "missing_profiles.json"),
    "MAPPING_JSON": str(TMP / "missing_mapping.json"),
    "CRAWLER_ENABLED": "0",
    "SECRET_KEY": "t" * 64,
    "ADMIN_PANEL_PASSWORD": "test_admin_pass",
    "SUPER_ADMIN_PASSWORD": "test_super_pass",
})
for k in ("USER_RECORDS_DB", "APPOINTMENTS_DB", "AI_KNOWLEDGE_DB"):
    print(f"  {k} -> {os.environ[k]}")

print("\n--- import app.main ---")
from app.db import init_all_dbs  # noqa: E402
import app.main  # noqa: E402
print("  OK: app.main وارد شد")

print("\n--- init_all_dbs() ---")
init_all_dbs()
print("  OK: بدون خطا")

import sqlite3  # noqa: E402
con = sqlite3.connect(f"file:{(TMP / 'user_records.db').as_posix()}?mode=ro", uri=True)
print("\n--- بعد از راه‌اندازی ---")
for r in con.execute("SELECT id, name FROM tenants ORDER BY id"):
    print(f"  مشتری {r[0]:>3} {r[1]}")
for r in con.execute("""SELECT p.id, p.name, p.mode, p.tier, p.message_quota, p.introduction_quota
                        FROM plans p ORDER BY p.id"""):
    print(f"  پلن   {r[0]:>3} {r[1]:<30} {r[2]:<9} tier={r[3]:<9} msg={r[4]:>5} intro={r[5]:>4}")
con.close()

a = sqlite3.connect(f"file:{(TMP / 'appointments.db').as_posix()}?mode=ro", uri=True)
cols = [c[1] for c in a.execute("PRAGMA table_info(appointments)")]
print(f"\n  نوبت‌ها: ستون tenant_id={'tenant_id' in cols}، جمع={a.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]}")
for r in a.execute("SELECT tenant_id, COUNT(*) FROM appointments GROUP BY tenant_id"):
    print(f"    tenant={r[0]} -> {r[1]} ردیف")
a.close()

print(f"\nکپی در: {TMP}")
print("دیتابیسِ زنده دست‌نخورده ماند.")
