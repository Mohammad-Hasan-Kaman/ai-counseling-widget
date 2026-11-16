# -*- coding: utf-8 -*-
"""تست‌های ریست ماهانهٔ شمارنده‌ها — «/ ماه» در جدول قیمت باید واقعاً ماهانه باشد."""
import uuid

import pytest

from app import plan_service
from app import tenants as tenants_mod


@pytest.fixture()
def tenant(dbs):
    suffix = uuid.uuid4().hex[:8]
    return tenants_mod.create_tenant(
        f"مرکز ماهانه {suffix}", f"mon_{suffix}", "MonPass123!"
    )


def _plan(name, msg_quota, intro_quota):
    return plan_service.create_plan(
        name, plan_service.MODE_MEDIUM, model="m",
        base_url="https://api.example.com/v1", api_key="sk-test",
        message_quota=msg_quota, introduction_quota=intro_quota, created_by="tester",
    )


def test_month_key_uses_tehran_time():
    """timestampها UTC ثبت می‌شوند؛ ماه باید به وقت تهران شمرده شود.

    ۲۰۲۶-۰۸-31 22:00 UTC = ۲۰۲۶-09-01 01:30 تهران → ماه سپتامبر.
    """
    assert plan_service.month_key("2026-08-31 22:00:00") == "2026-09"
    assert plan_service.month_key("2026-08-31 20:29:00") == "2026-08"
    assert plan_service.month_key("2026-09-01 00:00:00") == "2026-09"


def test_month_key_rejects_garbage():
    assert plan_service.month_key("") == ""
    assert plan_service.month_key(None) == ""
    assert plan_service.month_key("not-a-date") == ""


def test_reset_clears_counters_and_is_idempotent(dbs, tenant):
    """اولین اجرا شمارنده‌ها را صفر می‌کند، اجرای دوباره در همان ماه دست نمی‌زند"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("ماهانه", 500, 50), "tester")
    plan_service.increment_messages(tid)
    plan_service.increment_messages(tid)
    plan_service.increment_introductions(tid)
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 2

    first = plan_service.reset_expired_quotas()
    assert tid in first
    st = plan_service.get_tenant_plan_state(tid)
    assert st["messages_used"] == 0 and st["introductions_used"] == 0

    # در همان ماه: بار دوم نباید دوباره ریست کند (وگرنه مصرفِ همان ماه پاک می‌شد)
    plan_service.increment_messages(tid)
    assert plan_service.reset_expired_quotas() == []
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 1


def test_reset_preserves_previous_month_totals(dbs, tenant):
    """مصرف ماه قبل باید در تاریخچه بماند، نه اینکه بی‌سروصدا پاک شود"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("ماهانه تاریخچه", 500, 50), "tester")
    for _ in range(3):
        plan_service.increment_introductions(tid)
    plan_service.reset_expired_quotas()

    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT messages_used_at_change, introductions_used_at_change, month_key "
        "FROM plan_history WHERE tenant_id=? AND changed_by='monthly_reset'",
        (tid,),
    ).fetchone()
    conn.close()
    assert row is not None, "ردیف ریست ماهانه باید ثبت شود"
    assert row[1] == 3                      # مصرف معرفی پیش از ریست
    assert row[2] == plan_service._tehran_month()


def test_plan_reassignment_does_not_mark_month_as_reset(dbs, tenant):
    """تخصیص دوبارهٔ پلن نباید «این ماه ریست شده» را جعل کند.

    رگرسیون: اگر assign_plan کلید ماه را در جایی می‌نوشت، ریست بعدی رد
    می‌شد و مشتری یک ماه کامل بی‌سقف می‌ماند. assign_plan فقط شمارنده را
    صفر می‌کند، نه last_reset_month را."""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("الف", 500, 50), "tester")
    plan_service.reset_expired_quotas()          # اولین ریست ماه
    plan_service.increment_messages(tid)

    # همین ماه پلن عوض می‌شود (خودش شمارنده را صفر می‌کند)
    plan_service.assign_plan(tid, _plan("ب", 500, 50), "tester")

    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    last = conn.execute(
        "SELECT last_reset_month FROM tenant_plans WHERE tenant_id=?", (tid,)
    ).fetchone()[0]
    conn.close()
    assert last == plan_service._tehran_month()


def test_reset_fires_again_in_a_new_month(dbs, tenant, monkeypatch):
    """ماه که عوض شود، ریست باید دوباره اجرا شود"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("چرخه ماه", 500, 50), "tester")
    plan_service.reset_expired_quotas()
    plan_service.increment_messages(tid)
    assert plan_service.reset_expired_quotas() == [], "همان ماه: تکراری ریست نکن"

    # ماه بعد را شبیه‌سازی می‌کنیم: tenant ماه گذشته را ریست کرده
    monkeypatch.setattr(plan_service, "_tehran_month", lambda: "2020-01")
    assert tid in plan_service.reset_expired_quotas()
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 0


def test_quota_refills_next_month(dbs, tenant):
    """سقف که پر شده، بعد از ریست دوباره باز می‌شود"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("محدود", 2, 1), "tester")
    for _ in range(2):
        plan_service.increment_messages(tid)
    assert plan_service.check_lock(tid)[0] is True, "سقف ۲ پر شده → قفل"

    plan_service.reset_expired_quotas()
    assert plan_service.check_lock(tid)[0] is False, "بعد از ریست ماهانه باید باز شود"


def test_lazy_refill_db_error_never_breaks_chat(dbs, tenant, monkeypatch):
    """خطای دیتابیس در ریستِ تنبل نباید ویجت را بترکاند

    این کد روی مسیرِ درخواستِ کاربر اجرا می‌شود؛ قفلِ writers یا دیسکِ پر
    باید به یک «هنوز قفل است» ختم شود، نه ۵۰۰. job ساعتی همان کار را
    دوباره تلاش می‌کند."""
    import sqlite3 as _sqlite3

    class _LockedConn:
        """همهٔ نوشتن‌ها می‌ترکند — مثل قفلِ writers"""

        def execute(self, *_a, **_kw):
            raise _sqlite3.OperationalError("database is locked")

        def rollback(self):
            pass

        def commit(self):
            pass

    # باید False برگرداند و استثنا ندهد
    assert plan_service._refill_if_month_elapsed(
        _LockedConn(), 1, "2000-01") is False


def test_lazy_refill_without_prior_job_keeps_history_for_the_job(dbs, tenant, monkeypatch):
    """مشتریِ تازه (last_reset خالی) نباید تاریخچهٔ job را بدزدد

    اولین ریست باید توسط job انجام شود تا مصرفِ واقعِ قبل از ریست در
    plan_history ثبت شود؛ ریستِ تنبل فقط کارِ عقب‌افتاده را جبران می‌کند."""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("تازه", 500, 50), "tester")
    plan_service.increment_messages(tid)

    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT COUNT(*) FROM plan_history WHERE tenant_id=? AND changed_by='monthly_reset'",
        (tid,),
    ).fetchone()
    conn.close()
    # هیچ ریستی ثبت نشده تا job بیاید
    assert row[0] == 0
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 1, (
        "ریستِ تنبل نباید مشتریِ تازه را از مصرفِ همین ماه بیندازد")


def test_unlimited_never_locks(dbs, tenant):
    """پلن نامحدود با ریست هم باز می‌ماند"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(
        tid, _plan("نامحدود", plan_service.UNLIMITED, plan_service.UNLIMITED), "tester")
    for _ in range(5):
        plan_service.increment_messages(tid)
    plan_service.reset_expired_quotas()
    assert plan_service.check_lock(tid)[0] is False


def test_stale_lock_unlocks_without_the_scheduled_job(dbs, tenant, monkeypatch):
    """قفلِ به‌جامانده از ماهِ قبل، بدون job ساعتی هم باید باز شود

    رگرسیون: تنها راهِ بازشدنِ سقف، job ساعتی بود که فقط حین بالا بودنِ
    سرور اجرا می‌شد. سروری که شبِ آخر ماه خاموش شد، مشتری را تا روزها
    قفل نگه می‌داشت — یعنی سرویسِ خریده‌شده خاموش. این تست عمداً
    reset_expired_quotas را صدا نمی‌زند، یعنی همان حالتِ خاموش."""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("قفل کهنه", 2, 1), "tester")
    plan_service.reset_expired_quotas()          # ماهِ جاری ثبت شود
    for _ in range(2):
        plan_service.increment_messages(tid)
    assert plan_service.check_lock(tid) == (True, "message_quota"), "پیش‌نیاز: سقف پر"

    # ماه می‌گذرد، ولی job ساعتی هنوز نرسیده (سرور خاموش بوده)
    monkeypatch.setattr(plan_service, "_tehran_month", lambda: "2099-01")

    assert plan_service.check_lock(tid)[0] is False, (
        "قفلِ ماهِ گذشته باز نشد — مشتری سرویسِ پرداخت‌شده را نمی‌گیرد")
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 0


def test_stale_counters_reset_before_new_consumption(dbs, tenant, monkeypatch):
    """مصرفِ نوبتِ اولِ ماهِ جدید نباید روی شمارندهٔ ماهِ قبل بنشیند

    بدون ریستِ تنبل، increment_messages شمارندهٔ پرشدهٔ ماهِ قبل را
    می‌دید و مصرف را رد می‌کرد — مشتری در اولین پیامِ ماهِ جدید باز هم
    پیام هوشمند نمی‌گرفت."""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("مصرف", 2, 1), "tester")
    plan_service.reset_expired_quotas()
    for _ in range(2):
        plan_service.increment_messages(tid)

    monkeypatch.setattr(plan_service, "_tehran_month", lambda: "2099-01")

    assert plan_service.increment_messages(tid) is True, (
        "اولین مصرفِ ماهِ جدید رد شد — شمارندهٔ ماهِ قبل مانع بود")
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 1


def test_same_month_counter_is_not_reset(dbs, tenant, monkeypatch):
    """ریست تنبل نباید وسطِ همان ماه شمارنده را صفر کند"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("همان ماه", 10, 10), "tester")
    plan_service.reset_expired_quotas()
    plan_service.increment_messages(tid)
    plan_service.increment_messages(tid)

    plan_service.get_tenant_plan_state(tid)     # هنوز همان ماه است
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 2
    assert plan_service.increment_messages(tid) is True
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 3


def test_extra_topup_credit_survives_lazy_refill(dbs, tenant, monkeypatch):
    """اعتبارِ خریده‌شده نباید با ریستِ تنبل پاک شود

    اعتبار بستهٔ اضافه در extra_* است و ریست ماهانه دستش نمی‌زند؛
    مصرفش هم باید در ماهِ جدید ادامه پیدا کند."""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("بسته", 2, 1), "tester")
    plan_service.reset_expired_quotas()
    for _ in range(2):
        plan_service.increment_messages(tid)
    assert plan_service.check_lock(tid)[0] is True

    monkeypatch.setattr(plan_service, "_tehran_month", lambda: "2099-01")
    from app import db as db_mod
    from app.config import USER_RECORDS_DB
    conn = db_mod.get_conn(USER_RECORDS_DB)
    conn.execute("UPDATE tenant_plans SET extra_messages=10 WHERE tenant_id=?", (tid,))
    conn.commit()
    conn.close()

    state = plan_service.get_tenant_plan_state(tid)
    assert state["extra_messages"] == 10, "اعتبارِ خریده‌شده پاک شد"
    assert state["effective_message_quota"] == 12
    assert plan_service.check_lock(tid)[0] is False
