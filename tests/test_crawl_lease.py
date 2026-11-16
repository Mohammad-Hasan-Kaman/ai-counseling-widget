# -*- coding: utf-8 -*-
"""تست‌های lease شغلهای پس‌زمینه (توکن‌دار) + وضعیت کراول + ماندگاری گفتگوها"""
import time

from app import crawl_state as cs


def test_lease_acquire_release(dbs):
    job = "test_lease_a"
    cs.release(job)
    token = cs.try_acquire(job, ttl=60)
    assert token  # توکن مالکیت
    assert cs.try_acquire(job, ttl=60) is None  # قفل است
    assert cs.is_held(job) is True
    cs.release(job, token)
    assert cs.is_held(job) is False
    token2 = cs.try_acquire(job, ttl=60)
    assert token2
    cs.release(job)


def test_release_is_owner_scoped(dbs):
    """پایان یک اجرا نباید lease اجرای جدیدتر را حذف کند (یافته ریویو ۳)"""
    job = "test_lease_owner"
    cs.release(job)
    stale = cs.try_acquire(job, ttl=60)
    assert stale
    # اجرای جدید lease را تصاحب می‌کند (شبیه‌سازی: منقضی شدن قدیمی + گرفتن جدید)
    cs.release(job)  # قدیمی منقضی/آزاد شد
    fresh = cs.try_acquire(job, ttl=60)
    assert fresh and fresh != stale
    # اجرای قدیمی بعداً تمام می‌شود — نباید lease جدید را حذف کند
    cs.release(job, stale)
    assert cs.is_held(job) is True
    cs.release(job, fresh)
    assert cs.is_held(job) is False


def test_lease_expires_and_reacquirable(dbs):
    job = "test_lease_b"
    cs.release(job)
    assert cs.try_acquire(job, ttl=-1)  # منقضی از لحظه ساخت
    assert cs.try_acquire(job, ttl=60)  # lease منقضی → تصاحب جدید
    cs.release(job)


def test_renew_requires_matching_token(dbs):
    job = "test_lease_renew"
    cs.release(job)
    token = cs.try_acquire(job, ttl=60)
    assert token
    assert cs.renew(job, token, ttl=600) is True
    assert cs.renew(job, "some-other-token", ttl=600) is False  # توکن غلط نمی‌تواند تمدید کند
    cs.release(job)
    assert cs.renew(job, token, ttl=600) is False
    # lease منقضی: renew دوباره فعالش نمی‌کند
    expired = cs.try_acquire(job, ttl=-1)
    assert expired
    assert cs.renew(job, expired, ttl=600) is False
    cs.release(job, expired)


def test_crawl_progress_and_status(dbs):
    cs.ensure_tables()
    cs.release(cs.CRAWL_LEASE)
    cs.reset_progress()
    st = cs.get_status()
    assert st["running"] is False
    assert st["total"] == 0

    token = cs.try_acquire(cs.CRAWL_LEASE, 60)
    assert token
    cs.update_progress(done=10, total=86, current="page", free_total=3)
    st = cs.get_status()
    assert st["running"] is True  # از lease
    assert st["done"] == 10 and st["total"] == 86

    cs.update_progress(finished=True)
    cs.release(cs.CRAWL_LEASE, token)
    st = cs.get_status()
    assert st["running"] is False
    assert st["last_finished"] is not None


def test_gc_keeps_recent_expired_conversations(dbs):
    """سشن منقضی‌شده تا ۳۰ روز برای مشاهده ادمین در اینباکس می‌ماند (باگ سرور)"""
    from app.db import get_conn, init_widget_sessions_db, gc_expired_sessions
    from app.config import USER_RECORDS_DB
    from app import session_store as ss

    init_widget_sessions_db()
    token = ss.create_session(1)
    ss.log_message(token, "user", "سلام")
    ss.log_message(token, "bot", "سلام! خوش آمدید")

    conn = get_conn(USER_RECORDS_DB)
    # سشن ۲ روز پیش منقضی شده
    conn.execute(
        "UPDATE widget_sessions SET expires_at = datetime('now', '-2 days') WHERE token=?",
        (token,),
    )
    conn.commit()
    conn.close()

    gc_expired_sessions()

    conn = get_conn(USER_RECORDS_DB)
    sess = conn.execute("SELECT 1 FROM widget_sessions WHERE token=?", (token,)).fetchone()
    msgs = conn.execute(
        "SELECT COUNT(*) FROM widget_messages WHERE token=?", (token,)
    ).fetchone()[0]
    conn.close()
    assert sess is not None, "سشن منقضی‌شدهٔ اخیر نباید GC شود"
    assert msgs == 2, "پیام‌های گفتگو باید برای مشاهده ادمین بمانند"

    # سشن ۳۱ روز پیش منقضی (و پیام‌هایش هم قدیمی) → حذف کامل
    conn = get_conn(USER_RECORDS_DB)
    conn.execute(
        "UPDATE widget_sessions SET expires_at = datetime('now', '-31 days') WHERE token=?",
        (token,),
    )
    conn.execute(
        "UPDATE widget_messages SET created_at = datetime('now', '-31 days') WHERE token=?",
        (token,),
    )
    conn.commit()
    conn.close()
    gc_expired_sessions()
    conn = get_conn(USER_RECORDS_DB)
    sess = conn.execute("SELECT 1 FROM widget_sessions WHERE token=?", (token,)).fetchone()
    msgs = conn.execute(
        "SELECT COUNT(*) FROM widget_messages WHERE token=?", (token,)
    ).fetchone()[0]
    conn.close()
    assert sess is None
    assert msgs == 0


def test_engine_profile_cache_ttl_exists():
    """کش پروفایل موتور TTL دارد تا worker دیگر بعد از آپلود تازه بخواند"""
    import app.internal_ai_engine as eng
    from pathlib import Path
    assert eng.PROFILE_CACHE_TTL > 0
    e = eng.SpiralMatchEngine.__new__(eng.SpiralMatchEngine)
    eng.SpiralMatchEngine.__init__(e, Path("/nonexistent.json"))
    assert all(isinstance(v, tuple) and len(v) == 2 for v in e._tenant_cache.values())
