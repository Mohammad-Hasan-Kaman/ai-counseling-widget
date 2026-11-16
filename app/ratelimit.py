# -*- coding: utf-8 -*-
"""Rate limit مبتنی بر دیتابیس — بعد از ری‌استارت و با چند worker پایدار می‌ماند."""
import ipaddress
import time

from fastapi import Request

from app.config import USER_RECORDS_DB
from app.db import get_conn

_CREATE = """
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
)
"""
_ensured = False


def ensure_table():
    global _ensured
    if _ensured:
        return
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute(_CREATE)
        conn.commit()
        _ensured = True
    finally:
        conn.close()


def _trusted_proxy_peer(peer: str) -> bool:
    """فقط peer خصوصی/loopback (nginx هم‌میزبان یا شبکه داخلی) اعتماد می‌شود"""
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False  # testclient/نام‌میزبان غیرIP
    return bool(ip.is_loopback or ip.is_private)


def client_ip(request: Request) -> str:
    """پشت nginx: X-Forwarded-For فقط از proxy مورد اعتماد و فقط «آخرین هاپ»
    (همان‌که nginx با $proxy_add_x_forwarded_for اضافه کرده) — هاپ اول توسط
    کلاینت قابل جعل است و هرگز ملاک نیست. بدون proxy معتبر: آدرس مستقیم peer."""
    peer = request.client.host if request.client else ""
    if _trusted_proxy_peer(peer):
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1][:64]
    if peer:
        return peer
    return "unknown"


def rate_ok(key: str, limit: int, window: int) -> bool:
    """پنجره ثابت: حداکثر limit درخواست در هر window ثانیه به‌ازای هر کلید.
    BEGIN IMMEDIATE: خواندن-و-نوشتن اتمیک تا چند worker از سقف رد نشود."""
    ensure_table()
    now = time.time()
    conn = get_conn(USER_RECORDS_DB)
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT window_start, hits FROM rate_limits WHERE key=?", (key,)
        ).fetchone()
        if row is None or now - row[0] >= window:
            conn.execute(
                "INSERT INTO rate_limits(key, window_start, hits) VALUES (?, ?, 1) "
                "ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start, hits=1",
                (key, now),
            )
            conn.commit()
            committed = True
            return True
        hits = row[1]
        if hits >= limit:
            conn.commit()
            committed = True
            return False
        conn.execute("UPDATE rate_limits SET hits=? WHERE key=?", (hits + 1, key))
        conn.commit()
        committed = True
        return True
    finally:
        if not committed:
            try:
                conn.rollback()
            except Exception:
                pass
        conn.close()


def gc_old_keys(max_age: float = 86400.0):
    """حذف کلیدهای پنجره‌های قدیمی (توسط job ساعتی صدا زده می‌شود)"""
    ensure_table()
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (time.time() - max_age,))
        conn.commit()
    finally:
        conn.close()
