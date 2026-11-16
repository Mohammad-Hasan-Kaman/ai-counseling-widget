# -*- coding: utf-8 -*-
"""وضعیت کراول + lease شغلهای پس‌زمینه — دیتابیس‌محور تا چند worker ایمن باشد.

- crawl_state: پیشرفت مانیتورشونده (manual crawl polling)
- job_leases: قفل اتمی با TTL — جلوی اجرای همزمان کراول زمان‌بندی‌شده/دستی
  در چند worker و جلوی double-run زمان‌بند هر process را می‌گیرد؛
  با کرش process، lease بعد از TTL منقضی می‌شود (crash-safe).
"""
import logging
import os
import socket
import time
import uuid

from app.config import APPOINTMENTS_DB
from app.db import get_conn

logger = logging.getLogger(__name__)

CRAWL_LEASE = "crawl"
GC_LEASE = "gc"
CRAWL_LEASE_TTL = 900.0   # ~۵ دقیقه کراول + حاشیه امن (heartbeat هر صفحه تمدید می‌کند)
GC_LEASE_TTL = 300.0


def ensure_tables():
    conn = get_conn(APPOINTMENTS_DB)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_finished TEXT,
                started_at TEXT,
                total INTEGER DEFAULT 0,
                done INTEGER DEFAULT 0,
                current TEXT DEFAULT '',
                free_total INTEGER DEFAULT 0,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_leases (
                job TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        conn.execute("INSERT OR IGNORE INTO crawl_state (id) VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def _owner() -> str:
    """توکن یکتای هر بار گرفتن lease — release/renew فقط مالک خودش را می‌زند"""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def try_acquire(job: str, ttl: float) -> str | None:
    """گرفتن lease — خروجی: توکن مالکیت یا None اگر قفل باشد؛ اتمی با ON CONFLICT"""
    ensure_tables()
    now = time.time()
    token = _owner()
    conn = get_conn(APPOINTMENTS_DB)
    try:
        cur = conn.execute(
            "INSERT INTO job_leases(job, owner, expires_at) VALUES (?,?,?) "
            "ON CONFLICT(job) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at "
            "WHERE job_leases.expires_at < ?",
            (job, token, now + ttl, now),
        )
        conn.commit()
        if cur.rowcount > 0:
            return token
        logger.info("lease %s held by another run — skip", job)
        return None
    finally:
        conn.close()


def release(job: str, token: str | None = None):
    """آزادسازی — با توکن فقط lease خودمان حذف می‌شود؛ بدون توکن (ریست/تست) همه."""
    conn = get_conn(APPOINTMENTS_DB)
    try:
        if token:
            conn.execute("DELETE FROM job_leases WHERE job=? AND owner=?", (job, token))
        else:
            conn.execute("DELETE FROM job_leases WHERE job=?", (job,))
        conn.commit()
    finally:
        conn.close()


def renew(job: str, token: str | None = None, ttl: float = CRAWL_LEASE_TTL) -> bool:
    """تمدید lease فقط اگر هنوز مالکِ همان توکن باشد و منقضی نشده باشد (heartbeat)"""
    now = time.time()
    conn = get_conn(APPOINTMENTS_DB)
    try:
        if token:
            cur = conn.execute(
                "UPDATE job_leases SET expires_at=? WHERE job=? AND owner=? AND expires_at > ?",
                (now + ttl, job, token, now),
            )
        else:
            cur = conn.execute(
                "UPDATE job_leases SET expires_at=? WHERE job=? AND expires_at > ?",
                (now + ttl, job, now),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_held(job: str) -> bool:
    conn = get_conn(APPOINTMENTS_DB)
    try:
        row = conn.execute(
            "SELECT expires_at FROM job_leases WHERE job=?", (job,)
        ).fetchone()
        return bool(row and row[0] > time.time())
    finally:
        conn.close()


def reset_progress():
    """شروع کراول جدید — صفر کردن شمارنده‌ها"""
    ensure_tables()
    from datetime import datetime
    conn = get_conn(APPOINTMENTS_DB)
    try:
        conn.execute(
            "UPDATE crawl_state SET id=1, started_at=?, last_finished=last_finished, "
            "total=0, done=0, current='', free_total=0, error=NULL "
            "WHERE id=1",
            (datetime.now().strftime("%H:%M:%S"),),
        )
        if conn.total_changes == 0:
            conn.execute("INSERT INTO crawl_state (id, started_at) VALUES (1, ?)",
                         (datetime.now().strftime("%H:%M:%S"),))
        conn.commit()
    finally:
        conn.close()


def update_progress(done: int = None, total: int = None, current: str = None,
                    free_total: int = None, error: str = None, finished: bool = False):
    ensure_tables()
    from datetime import datetime
    sets, args = [], []
    if done is not None:
        sets.append("done=?"); args.append(done)
    if total is not None:
        sets.append("total=?"); args.append(total)
    if current is not None:
        sets.append("current=?"); args.append(current[:120])
    if free_total is not None:
        sets.append("free_total=?"); args.append(free_total)
    if error is not None:
        sets.append("error=?"); args.append(error[:200])
    if finished:
        sets.append("last_finished=?")
        args.append(datetime.now().strftime("%H:%M:%S"))
    if not sets:
        return
    conn = get_conn(APPOINTMENTS_DB)
    try:
        conn.execute(f"UPDATE crawl_state SET {', '.join(sets)} WHERE id=1", args)
        conn.commit()
    finally:
        conn.close()


def get_status() -> dict:
    """وضعیت برای polling پنل — running از lease محاسبه می‌شود (کرش‌امن).
    جدول‌ها در init_all_dbs ساخته می‌شوند؛ اینجا فقط خواندن (بدون نوشتن هر poll)."""
    conn = get_conn(APPOINTMENTS_DB)
    try:
        row = conn.execute(
            "SELECT last_finished, started_at, total, done, current, free_total, error "
            "FROM crawl_state WHERE id=1"
        ).fetchone()
    finally:
        conn.close()
    data = {
        "running": is_held(CRAWL_LEASE),
        "last_finished": None, "started_at": None,
        "total": 0, "done": 0, "current": "", "free_total": 0, "error": None,
    }
    if row:
        data.update({
            "last_finished": row[0], "started_at": row[1],
            "total": row[2] or 0, "done": row[3] or 0,
            "current": row[4] or "", "free_total": row[5] or 0, "error": row[6],
        })
    return data
