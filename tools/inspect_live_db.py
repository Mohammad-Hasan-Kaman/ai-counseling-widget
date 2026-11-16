# -*- coding: utf-8 -*-
"""گزارشِ فقط‌خواندنی از دیتابیسِ زنده — برای تصمیمِ پاک‌سازی.

هیچ چیزی را تغییر نمی‌دهد: با file:...?mode=ro باز می‌شود.
اجرا:  PYTHONIOENCODING=utf-8 python tools/inspect_live_db.py
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "user_records.db"

con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def out(*a):
    print(*a, file=sys.stdout, flush=True)


out(f"دیتابیس: {DB}  ({DB.stat().st_size:,} بایت)")

# ── مشتری‌ها ──
out("\n=== مشتری‌ها ===")
for r in con.execute("""
    SELECT t.id, t.name, t.slug, t.active, t.created_at,
           COALESCE((SELECT p.name FROM tenant_plans tp JOIN plans p ON p.id=tp.plan_id
                     WHERE tp.tenant_id=t.id), '-') AS plan_name,
           (SELECT COUNT(*) FROM user_requests u WHERE u.tenant_id=t.id) AS requests,
           (SELECT COUNT(*) FROM widget_sessions w WHERE w.tenant_id=t.id) AS sessions,
           (SELECT COUNT(*) FROM widget_messages m WHERE m.tenant_id=t.id) AS messages,
           (SELECT COUNT(*) FROM panel_users p WHERE p.tenant_id=t.id) AS admins
    FROM tenants t ORDER BY t.id"""):
    out(f"  {r['id']:>3}  {r['name'][:26]:<26} slug={r['slug'][:20]:<20} "
        f"{str(r['created_at'])[:19]:<19} plan={r['plan_name'][:20]:<20} "
        f"req={r['requests']} sess={r['sessions']} msg={r['messages']} adm={r['admins']}")

# ── پلن‌ها ──
out("\n=== پلن‌ها ===")
for r in con.execute("""
    SELECT p.id, p.name, p.mode, p.tier, p.message_quota, p.introduction_quota, p.active,
           (SELECT COUNT(*) FROM tenant_plans tp WHERE tp.plan_id=p.id) AS used_by,
           (SELECT group_concat(tp.tenant_id) FROM tenant_plans tp WHERE tp.plan_id=p.id) AS tenants
    FROM plans p ORDER BY p.id"""):
    out(f"  {r['id']:>3}  {r['name'][:30]:<30} {r['mode']:<9} tier={r['tier']:<9} "
        f"msg={r['message_quota']:>6} intro={r['introduction_quota']:>5} "
        f"active={r['active']} used_by={r['used_by']} tenants={r['tenants']}")

# ── ادمین‌ها ──
out("\n=== ادمین‌ها (panel_users) ===")
for r in con.execute(
        "SELECT username, role, tenant_id, active FROM panel_users ORDER BY tenant_id, username"):
    out(f"  {r['username']:<24} role={r['role']:<12} tenant={r['tenant_id']} active={r['active']}")

# ── جدول‌های وابسته ──
out("\n=== جدول‌های دیگر ===")
for tbl in ("panel_users", "user_requests", "widget_sessions", "widget_messages",
            "consultants", "users", "announcements", "topup_requests",
            "topup_packages", "plan_history", "rate_limits", "tenant_plans"):
    try:
        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    except sqlite3.Error as e:
        out(f"  {tbl:<20} (خطا: {e})")
        continue
    out(f"  {tbl:<20} {n}")

con.close()
