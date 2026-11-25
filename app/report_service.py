# -*- coding: utf-8 -*-
"""گزارش دوره‌ای برای پنل مشتری — فقط پلن «حرفه‌ای».

- همهٔ created_atها با CURRENT_TIMESTAMP ثبت شده‌اند (UTC)؛ در SQL با
  '+210 minutes' به وقت تهران (UTC+3:30، بدون DST از ۲۰۲۲) برده می‌شوند —
  نه 'localtime' چون در Docker متغیر TZ ست نیست و UTC برمی‌گرداند.
- widget_sessions/widget_messages حداکثر ~۳۱ روز نگه داشته می‌شوند
  (gc_expired_sessions)، پس متریک‌های «گفتگو» در بازهٔ ۹۰ روز ناقص‌اند
  (partial_conversations=True → رندر با «—» و footnote). user_requests پاک نمی‌شود.
"""
from datetime import datetime, timedelta, timezone

from app.config import USER_RECORDS_DB
from app.db import get_conn
from app import plan_service

REPORT_DAYS = (7, 30, 90)
DEFAULT_DAYS = 7
TEHRAN = timezone(timedelta(minutes=210))
# نگهداری واقعی جداول گفتگو (app/db.py gc_expired_sessions)
CONVERSATION_RETENTION_DAYS = 30


def sanitize_days(raw) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return v if v in REPORT_DAYS else DEFAULT_DAYS


def _tehran_today() -> str:
    return datetime.now(TEHRAN).date().isoformat()


def _day_axis(days: int) -> list[str]:
    """محور روزها به‌صورت کامل (zero-fill) — قدیمی‌ترین اول، به وقت تهران."""
    today = datetime.now(TEHRAN).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _zero_fill(axis: list[str], rows: dict) -> dict:
    return {d: int(rows.get(d, 0)) for d in axis}


def build_report(tenant_id: int, days: int) -> dict:
    """آمار دورهٔ `days` روز اخیر. خروجی: dict آمادهٔ رندر/اکسل."""
    days = sanitize_days(days)
    axis = _day_axis(days)
    conn = get_conn(USER_RECORDS_DB)
    try:
        cutoff = f"-{days} days"

        def _count(table: str) -> int:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE tenant_id=? AND created_at >= datetime('now', ?)",
                (tenant_id, cutoff),
            ).fetchone()[0]

        conversations = _count("widget_sessions")
        leads = _count("user_requests")

        # مشاوران معرفی‌شده: recommendations خالی/لیست‌خالی = بدون مشاوره
        consultations = conn.execute(
            "SELECT COUNT(*) FROM user_requests "
            "WHERE tenant_id=? AND created_at >= datetime('now', ?) "
            "AND recommendations IS NOT NULL AND recommendations NOT IN ('','[]')",
            (tenant_id, cutoff),
        ).fetchone()[0]

        # GHQ: AVG روی بدون‌ردیف → NULL → رندر «—»
        ghq_avg, ghq_n = conn.execute(
            "SELECT AVG(ghq_total), COUNT(ghq_total) FROM user_requests "
            "WHERE tenant_id=? AND created_at >= datetime('now', ?)",
            (tenant_id, cutoff),
        ).fetchone()

        # سری روزانه — گروه‌بندی با وقت تهران
        def _series(table: str, where: str = "") -> dict:
            rows = conn.execute(
                f"SELECT date(created_at, '+210 minutes') AS d, COUNT(*) FROM {table} "
                f"WHERE tenant_id=? AND created_at >= datetime('now', ?) {where} "
                f"GROUP BY d",
                (tenant_id, cutoff),
            ).fetchall()
            return _zero_fill(axis, dict(rows))

        daily = {
            "conversations": _series("widget_sessions"),
            "leads": _series("user_requests"),
            "consultations": _series(
                "user_requests",
                "AND recommendations IS NOT NULL AND recommendations NOT IN ('','[]')",
            ),
        }

        top_topics = [
            {"name": (r[0] or "—"), "count": int(r[1])}
            for r in conn.execute(
                "SELECT topic, COUNT(*) FROM user_requests "
                "WHERE tenant_id=? AND created_at >= datetime('now', ?) "
                "GROUP BY topic ORDER BY 2 DESC, 1 LIMIT 5",
                (tenant_id, cutoff),
            ).fetchall()
            if r[0]
        ]

        rec_rows = conn.execute(
            "SELECT recommendations FROM user_requests "
            "WHERE tenant_id=? AND created_at >= datetime('now', ?) "
            "AND recommendations IS NOT NULL AND recommendations NOT IN ('','[]')",
            (tenant_id, cutoff),
        ).fetchall()
    finally:
        conn.close()

    # همان parse قدیمی توصیه‌ها (app/admin_tools._fmt_recs) — «نام | نام»
    from app.admin_tools import _fmt_recs
    counts: dict[str, int] = {}
    for (rec_json,) in rec_rows:
        names, _reasons = _fmt_recs(rec_json)
        for n in names.split(" | "):
            n = n.strip()
            if n:
                counts[n] = counts.get(n, 0) + 1
    top_consultants = sorted(
        ({"name": n, "count": c} for n, c in counts.items()),
        key=lambda x: (-x["count"], x["name"]),
    )[:5]

    return {
        "days": days,
        "axis": axis,
        "tehran_today": _tehran_today(),
        "conversations": conversations,
        "leads": leads,
        "consultations": consultations,
        "ghq_avg": (round(float(ghq_avg), 1) if ghq_avg is not None else None),
        "ghq_n": int(ghq_n or 0),
        "top_topics": top_topics,
        "top_consultants": top_consultants,
        "daily": daily,
        # گفتگوها حداکثر ~۳۰ روز نگه داشته می‌شوند → در بازهٔ ۹۰ ناقص‌اند
        "partial_conversations": days > CONVERSATION_RETENTION_DAYS,
        "retention_days": CONVERSATION_RETENTION_DAYS,
    }
