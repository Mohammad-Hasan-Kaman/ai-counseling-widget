# -*- coding: utf-8 -*-
import sqlite3
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
DB = Path("data/user_records.db")
APPT = Path("data/appointments.db")
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
print("=== مشتریانِ باقی‌مانده ===")
for r in con.execute("SELECT id, name, slug, active FROM tenants ORDER BY id"):
    print(f"  {r['id']:>3}  {r['name']:<26} {r['slug']}")
print("\n=== پلن‌های باقی‌مانده ===")
for r in con.execute("""SELECT p.id, p.name, p.mode, p.tier, p.message_quota, p.introduction_quota,
   (SELECT COUNT(*) FROM tenant_plans tp WHERE tp.plan_id=p.id) used
   FROM plans p ORDER BY p.id"""):
    print(f"  {r['id']:>3}  {r['name']:<30} {r['mode']:<9} tier={r['tier']:<9} "
          f"msg={r['message_quota']:>5} intro={r['introduction_quota']:>4} استفاده={r['used']}")
print("\n=== تخصیص پلن ===")
for r in con.execute("""SELECT tp.tenant_id, t.name, p.name plan, p.message_quota, p.introduction_quota,
   tp.messages_used, tp.introductions_used FROM tenant_plans tp
   JOIN tenants t ON t.id=tp.tenant_id JOIN plans p ON p.id=tp.plan_id ORDER BY tp.tenant_id"""):
    print(f"  tenant {r['tenant_id']} {r['name'][:20]:<20} plan={r['plan'][:26]:<26} "
          f"msg={r['message_quota']}/{r['messages_used']} int={r['introduction_quota']}/{r['introductions_used']}")
print("\n=== یتیم‌ها (باید همه صفر باشند) ===")
ids = [r[0] for r in con.execute("SELECT id FROM tenants")]
ph = ",".join("?" * len(ids))
for t in ("panel_users", "tenant_plans", "plan_history", "widget_sessions",
          "widget_messages", "user_requests", "users", "consultants"):
    cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
    if "tenant_id" not in cols:
        continue
    n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE tenant_id NOT IN ({ph})", ids).fetchone()[0]
    print(f"  {t:<20} یتیم/بی‌مالک: {n}")
sess = [r[0] for r in con.execute("SELECT token FROM widget_sessions")]
if sess:
    n = con.execute(f"SELECT COUNT(*) FROM widget_messages WHERE token NOT IN "
                    f"({','.join('?' * len(sess))})", sess).fetchone()[0]
    print(f"  widget_messages بی‌سشن: {n}")
print("\n=== دادهٔ tenant اصلی دست‌نخورده است؟ ===")
for t in ("user_requests", "widget_sessions", "widget_messages", "users", "consultants", "announcements"):
    cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
    if "tenant_id" in cols:
        n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE tenant_id=1").fetchone()[0]
        print(f"  {t:<20} tenant=1: {n}")
print("\n=== کلیدهای API باقی‌مانده ===")
for r in con.execute("SELECT id, name, api_key FROM tenants ORDER BY id"):
    print(f"  {r['id']:>3} {r['name'][:24]:<24} {r['api_key'][:14]}...")
con.close()
if APPT.exists():
    a = sqlite3.connect(f"file:{APPT.as_posix()}?mode=ro", uri=True)
    cols = [c[1] for c in a.execute("PRAGMA table_info(appointments)")]
    print(f"\nappointments: ستون tenant_id موجود است؟ {'tenant_id' in cols}  (ستون در اجرای بعدیِ اپ ساخته می‌شود)")
    print(f"  تعداد کل: {a.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]}")
    a.close()
