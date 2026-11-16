# -*- coding: utf-8 -*-
"""تست‌های سقف اختصاصیِ مشتری و بستهٔ اضافه (top-up).

«سقف کاملاً سفارشی» در جدول قیمت یعنی سقف روی خودِ مشتری، نه روی پلن —
وگرنه ویرایش یک پلنِ مشترک retroactively روی بقیهٔ مشتری‌های آن پلن هم
اثر می‌گذاشت."""
import uuid

import pytest

from app import plan_service
from app import tenants as tenants_mod


@pytest.fixture()
def tenant(dbs):
    suffix = uuid.uuid4().hex[:8]
    created = tenants_mod.create_tenant(
        f"مرکز سقف {suffix}", f"quota_{suffix}", "QuotaPass123!"
    )
    created["admin_username"] = f"quota_{suffix}"
    return created


def _plan(message_quota=500, introduction_quota=50):
    return plan_service.create_plan(
        f"استاندارد {uuid.uuid4().hex[:6]}", plan_service.MODE_MEDIUM,
        model="m", base_url="https://api.example.com/v1", api_key="sk-x",
        message_quota=message_quota, introduction_quota=introduction_quota,
        created_by="tester",
    )


# ── سقف اختصاصی ──

def test_override_replaces_plan_quota(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    plan_service.set_quota_override(tid, "200", "10", "tester")

    state = plan_service.get_tenant_plan_state(tid)
    assert state["effective_message_quota"] == 200
    assert state["effective_introduction_quota"] == 10
    # سقف خودِ پلن دست‌نخورده می‌ماند
    assert state["plan"]["message_quota"] == 500


def test_blank_override_inherits_plan(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    plan_service.set_quota_override(tid, "200", "10", "tester")
    plan_service.set_quota_override(tid, "", "", "tester")  # خالی = برگشت به پلن

    state = plan_service.get_tenant_plan_state(tid)
    assert state["message_quota_override"] is None
    assert state["effective_message_quota"] == 500


def test_override_only_affects_this_tenant(dbs, tenant):
    """سقف اختصاصی نباید مشتریِ دیگرِ همان پلن را هم عوض کند"""
    other = tenants_mod.create_tenant(
        f"مرکز دوم {uuid.uuid4().hex[:6]}", f"other_{uuid.uuid4().hex[:6]}", "OtherPass123!")
    plan_id = _plan(500, 50)
    plan_service.assign_plan(tenant["tenant_id"], plan_id, "tester")
    plan_service.assign_plan(other["tenant_id"], plan_id, "tester")

    plan_service.set_quota_override(tenant["tenant_id"], "7", "3", "tester")
    assert plan_service.get_tenant_plan_state(
        other["tenant_id"])["effective_message_quota"] == 500


def test_unlimited_override_and_plan(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    plan_service.set_quota_override(tid, "-1", "-1", "tester")
    state = plan_service.get_tenant_plan_state(tid)
    assert state["effective_message_quota"] == -1
    assert plan_service.check_lock(tid) == (False, "")


def test_override_rejects_bad_values(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    with pytest.raises(ValueError):
        plan_service.set_quota_override(tid, "abc", "", "tester")
    with pytest.raises(ValueError):
        plan_service.set_quota_override(tid, "-5", "", "tester")


def test_override_without_plan_rejected(dbs, tenant):
    with pytest.raises(ValueError):
        plan_service.set_quota_override(tenant["tenant_id"], "10", "", "tester")


# ── بستهٔ اضافه ──

def test_topup_package_adds_to_effective_quota(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    pkg = plan_service.create_package("۱۰۰۰ پیام", 1000, 10, 12_900_000, "tester")

    req_id = plan_service.request_topup(tid, pkg, "admin_user")
    # خرید به‌تنهایی سرویس را باز نمی‌کند — پرداخت هنوز تأیید نشده
    assert plan_service.get_tenant_plan_state(tid)["effective_message_quota"] == 500

    plan_service.resolve_topup(req_id, True, "super")
    state = plan_service.get_tenant_plan_state(tid)
    assert state["extra_messages"] == 1000
    assert state["effective_message_quota"] == 1500
    assert state["effective_introduction_quota"] == 60


def test_rejected_topup_grants_nothing(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    pkg = plan_service.create_package("۱۰۰۰ پیام", 1000, 10, 12_900_000, "tester")
    plan_service.resolve_topup(plan_service.request_topup(tid, pkg, "u"), False, "super")
    state = plan_service.get_tenant_plan_state(tid)
    assert state["extra_messages"] == 0
    assert state["effective_message_quota"] == 500


def test_topup_survives_monthly_reset(dbs, tenant):
    """اعتبارِ خریده‌شده با ریست ماهانه پاک نمی‌شود — «میزانی که خریدی» است"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    pkg = plan_service.create_package("بسته", 1000, 10, 1_000_000, "tester")
    plan_service.resolve_topup(plan_service.request_topup(tid, pkg, "u"), True, "super")
    plan_service.reset_expired_quotas("tester")

    state = plan_service.get_tenant_plan_state(tid)
    assert state["messages_used"] == 0
    assert state["extra_messages"] == 1000
    assert state["effective_message_quota"] == 1500


def test_topup_unlocks_a_locked_tenant(dbs, tenant):
    """وقتی سقف پر شده، خرید بسته باید ویجت را دوباره باز کند"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(2, 50), "tester")
    assert plan_service.increment_messages(tid) is True
    assert plan_service.increment_messages(tid) is True
    assert plan_service.check_lock(tid) == (True, "message_quota")

    pkg = plan_service.create_package("بسته", 100, 0, 1_000_000, "tester")
    plan_service.resolve_topup(plan_service.request_topup(tid, pkg, "u"), True, "super")
    assert plan_service.check_lock(tid) == (False, "")
    assert plan_service.increment_messages(tid) is True


def test_resolve_twice_rejected(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    pkg = plan_service.create_package("بسته", 100, 0, 1_000_000, "tester")
    req = plan_service.request_topup(tid, pkg, "u")
    plan_service.resolve_topup(req, True, "super")
    with pytest.raises(ValueError):
        plan_service.resolve_topup(req, True, "super")
    # اعتبار دوباره اضافه نشد
    assert plan_service.get_tenant_plan_state(tid)["extra_messages"] == 100


def test_snapshot_survives_package_edit(dbs, tenant):
    """ویرایش/غیرفعال‌کردن بسته، درخواستِ در انتظار را جابه‌جا نمی‌کند"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    pkg = plan_service.create_package("بسته", 100, 0, 1_000_000, "tester")
    req = plan_service.request_topup(tid, pkg, "u")
    plan_service.set_package_active(pkg, False, "super")

    pending = [r for r in plan_service.list_topup_requests(None, "pending")
               if r["id"] == req]
    assert len(pending) == 1
    assert pending[0]["extra_messages"] == 100
    plan_service.resolve_topup(req, True, "super")
    assert plan_service.get_tenant_plan_state(tid)["extra_messages"] == 100


def test_inactive_package_not_buyable(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    pkg = plan_service.create_package("بسته", 100, 0, 1_000_000, "tester")
    plan_service.set_package_active(pkg, False, "super")
    with pytest.raises(ValueError):
        plan_service.request_topup(tid, pkg, "u")
    assert pkg not in {p["id"] for p in plan_service.list_packages()}


def test_empty_package_rejected(dbs):
    with pytest.raises(ValueError):
        plan_service.create_package("خالی", 0, 0, 1000, "t")
    with pytest.raises(ValueError):
        plan_service.create_package("", 10, 0, 1000, "t")


def test_approve_without_plan_does_not_silently_swallow_credit(dbs, tenant):
    """مشتریِ بدون پلن نباید بتواند اصلاً خرید کند، و اگر درخواستی somehow
    ثبت شد، تأییدش نباید اعتبار را بی‌صدا دور بریزد: UPDATE روی tenant_plansِ
    بدون ردیف ۰ سطر می‌خورد و پیام «موفق» می‌دهد."""
    tid = tenant["tenant_id"]
    pkg = plan_service.create_package("بسته", 500, 10, 1_000_000, "tester")
    with pytest.raises(ValueError):
        plan_service.request_topup(tid, pkg, "u")   # خرید بدون پلن رد می‌شود

    # حتی اگر درخواستی از راه دیگری ثبت شده باشد، تأیید نباید اعتبار را گم کند
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    c = get_conn(USER_RECORDS_DB)
    c.execute(
        "INSERT INTO topup_requests (tenant_id, package_id, package_name, "
        "extra_messages, extra_introductions, price_rials) VALUES (?,?,?,?,?,?)",
        (tid, pkg, "بسته", 500, 10, 1_000_000))
    c.commit(); c.close()
    req = next(r["id"] for r in plan_service.list_topup_requests(tid, "pending"))
    with pytest.raises(ValueError):
        plan_service.resolve_topup(req, True, "super")
    # درخواست باید همچنان pending بماند تا بعد از تخصیص پلن قابل تأیید باشد
    still = [r for r in plan_service.list_topup_requests(tid, "pending") if r["id"] == req]
    assert len(still) == 1, "درخواستِ ردشده نباید pending بماند یا برعکس"


def test_huge_quota_and_package_values_rejected(dbs, tenant):
    """عددِ بزرگ‌تر از INTEGERِ SQLite با OverflowError می‌ترکد که روت‌ها
    نمی‌گیرند (ValueError نیست) → ۵۰۰. باید پیش از رسیدن به DB رد شود."""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    with pytest.raises(ValueError):
        plan_service.set_quota_override(tid, "99999999999999999999", "", "t")
    with pytest.raises(ValueError):
        plan_service.create_package("هولدر", 10**20, 0, 1000, "t")
    with pytest.raises(ValueError):
        plan_service.create_package("هولدر", 10, 0, 10**20, "t")
    # سقفِ بزرگ ولی معتبر پذیرفته شود
    plan_service.set_quota_override(tid, "1000000", "", "t")
    assert plan_service.get_tenant_plan_state(tid)["effective_message_quota"] == 1000000


def test_list_topup_requests_never_leaks_across_tenants(dbs, tenant):
    """فیلترِ tenant باید WHERE باشد نه AND. «AND» بعد از LEFT JOIN به شرطِ
    ON می‌چسبد و در LEFT JOIN ردیفِ سمتِ چپ را حذف نمی‌کند — یعنی هر مشتری
    سوابق خریدِ همهٔ مشتری‌ها (نام بسته، مبلغ، وضعیت) را می‌دید."""
    other = tenants_mod.create_tenant(
        f"مرکز دیگر {uuid.uuid4().hex[:6]}", f"other_{uuid.uuid4().hex[:6]}", "OtherPass123!")
    mine_id, other_id = tenant["tenant_id"], other["tenant_id"]
    plan_service.assign_plan(mine_id, _plan(500, 50), "t")
    plan_service.assign_plan(other_id, _plan(500, 50), "t")
    pkg_mine = plan_service.create_package("بستهٔ من", 100, 5, 5_000_000, "t")
    pkg_other = plan_service.create_package("بستهٔ آن‌ها", 999, 9, 99_000_000, "t")
    plan_service.request_topup(mine_id, pkg_mine, "u")
    plan_service.request_topup(other_id, pkg_other, "u")

    for status in ("", "pending", None):
        seen = {r["package_name"] for r in plan_service.list_topup_requests(mine_id, status)}
        assert seen == {"بستهٔ من"}, f"status={status!r}: نشت بین مشتری‌ها — {seen}"
        assert all(r["tenant_id"] == mine_id
                   for r in plan_service.list_topup_requests(mine_id, status))

    # سوپرادمین (tenant_id=None) باید همه را ببیند
    assert len(plan_service.list_topup_requests(None, "pending")) >= 2


# ── مسیرها ──

def _clear_login_rate_limit():
    """سقف تلاش ورود (۱۰ در ۵ دقیقه به‌ازای IP) روی سشن مشترک تست می‌نشیند:
    چندین فایل تست با همان IP لاگین می‌کنند و آخرین تستِ ورودی بی‌دلیل ۴۲۹
    می‌گیرد. کلید پاک می‌شود تا نتیجهٔ تست از ترتیب اجرا مستقل بماند."""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute("DELETE FROM rate_limits WHERE key='login:testclient'")
        conn.commit()
    finally:
        conn.close()


def _login(client, tenant):
    _clear_login_rate_limit()
    r = client.post(
        "/admin/login",
        data={"username": tenant["admin_username"], "password": "QuotaPass123!"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def test_topup_page_and_purchase_flow(dbs, tenant):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    _login(client, tenant)
    plan_service.assign_plan(tenant["tenant_id"], _plan(500, 50), "tester")
    pkg = plan_service.create_package("۵۰۰ پیام", 500, 5, 4_900_000, "tester")

    r = client.get("/admin/topups")
    assert r.status_code == 200, r.text
    assert "۵۰۰ پیام" in r.text
    assert plan_service.rial_label(4_900_000) in r.text  # ۴٬۹۰۰٬۰۰۰ تومان

    r = client.post("/admin/topups/buy", data={"package_id": pkg},
                    headers={"Origin": "http://testserver"}, follow_redirects=True)
    assert r.status_code == 200
    assert "در انتظار تأیید" in r.text
    # هنوز اعتباری اضافه نشده
    assert plan_service.get_tenant_plan_state(
        tenant["tenant_id"])["effective_message_quota"] == 500


def test_topup_page_requires_login(dbs):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.get("/admin/topups", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_topup_page_blocked_for_superadmin(dbs, tenant):
    """سوپرادمین به فروشگاهِ مشتری دسترسی ندارد — کنترلِ خودِ مشتری"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.get("/admin/topups", follow_redirects=False)
    assert r.status_code in (303, 403)


# ── پنل سوپرادمین ──

def _super_client():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    _clear_login_rate_limit()
    r = client.post(
        "/admin/login",
        data={"username": "superadmin", "password": "test_super_pass"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return client


def test_lock_message_rejected_for_superadmin(dbs):
    """سوپرادمین tenant ندارد؛ فرم متنِ قفل روی id=NULL صفر سطر می‌نوشت و
    بی‌صدا ۳۰۳ موفق برمی‌گرداند."""
    client = _super_client()
    r = client.post("/admin/lock-message", data={"lock_message": "x"},
                    headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 403, r.text


def test_llm_test_connection_route_not_shadowed(dbs):
    """/super/plans/test-connection نباید زیرِ /super/plans/{plan_id} گم شود.
    FastAPI به ترتیب ثبت مسیرها match می‌کند، پس مسیرِ ثابت باید اول بیاید —
    وگرنه «تست اتصال» ۴۲۲ می‌گیرد و اصلاً به LLM نمی‌رسد."""
    from fastapi.testclient import TestClient
    from app.main import app
    client = _super_client()
    r = client.post(
        "/admin/super/plans/test-connection",
        data={"base_url": "https://api.invalid/v1", "model": "some-model", "api_key": "sk-x"},
        headers={"Origin": "http://testserver"}, follow_redirects=False,
    )
    assert r.status_code == 200, f"{r.status_code} — مسیر زیرِ مسیرِ پارامتری افتاده: {r.text[:200]}"


def test_superadmin_topups_page_renders(dbs, tenant):
    """صفحهٔ بستهٔ اضافه باید رندر شود و درخواستِ معلق را نشان دهد"""
    plan_service.assign_plan(tenant["tenant_id"], _plan(500, 50), "tester")
    pkg = plan_service.create_package("۵۰۰ پیام", 500, 5, 4_900_000, "tester")
    plan_service.request_topup(tenant["tenant_id"], pkg, "quota_admin")

    client = _super_client()
    r = client.get("/admin/super/topups")
    assert r.status_code == 200, r.text
    assert "ساخت بستهٔ اضافه" in r.text
    assert "۵۰۰ پیام" in r.text
    assert plan_service.rial_label(4_900_000) in r.text
    assert "در انتظار تأیید" in r.text


def test_superadmin_approve_flow(dbs, tenant):
    """تأیید از پنل سوپرادمین باید اعتبار را واقعاً اضافه کند"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    pkg = plan_service.create_package("بستهٔ تست", 250, 0, 1_000_000, "tester")
    req = plan_service.request_topup(tid, pkg, "quota_admin")

    client = _super_client()
    r = client.post(
        f"/admin/super/topups/{req}/resolve", data={"approve": "1"},
        headers={"Origin": "http://testserver"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert plan_service.get_tenant_plan_state(tid)["effective_message_quota"] == 750


def test_quota_override_route(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan(500, 50), "tester")
    client = _super_client()
    r = client.post(
        f"/admin/super/tenants/{tid}/quota-override",
        data={"message_quota": "42", "introduction_quota": ""},
        headers={"Origin": "http://testserver"}, follow_redirects=False,
    )
    assert r.status_code == 200, r.text  # داشبورد با پیام تأیید
    assert "سقف اختصاصی" in r.text
    state = plan_service.get_tenant_plan_state(tid)
    assert state["effective_message_quota"] == 42
    assert state["effective_introduction_quota"] == 50  # خالی → ارث از پلن
