# -*- coding: utf-8 -*-
"""تست‌های سیستم پلن/سقف — plan_service، رمزنگاری، قفل، audit"""
import uuid

import pytest

from app import plan_service, encryption_service, llm_service
from app import tenants as tenants_mod


@pytest.fixture()
def tenant(dbs):
    suffix = uuid.uuid4().hex[:8]
    created = tenants_mod.create_tenant(
        f"مرکز پلن‌دار {suffix}", f"plan_admin_{suffix}", "PlanPass123!"
    )
    return created


def test_encryption_roundtrip():
    token = encryption_service.encrypt_str("sk-secret-123")
    assert token != "sk-secret-123"
    assert encryption_service.decrypt_str(token) == "sk-secret-123"
    assert encryption_service.decrypt_str("") == ""


def test_create_plan_and_public_view(dbs):
    pid = plan_service.create_plan(
        "پلن ضعیف تست", plan_service.MODE_WEAK,
        introduction_quota=50, created_by="tester",
    )
    plan = plan_service.get_plan(pid)
    assert plan["mode"] == "weak"
    assert plan["introduction_quota"] == 50
    assert plan["message_quota"] == plan_service.UNLIMITED
    # weak کلید LLM ندارد
    assert plan["api_key_encrypted"] == ""


def test_create_medium_requires_llm_fields(dbs):
    with pytest.raises(ValueError, match="base_url"):
        plan_service.create_plan("ناقص", plan_service.MODE_MEDIUM)
    pid = plan_service.create_plan(
        "متوسط کامل", plan_service.MODE_MEDIUM,
        model="deepseek-chat", base_url="https://api.example.com/v1",
        api_key="sk-test", message_quota=100, introduction_quota=50,
    )
    plan = plan_service.get_plan(pid)
    assert plan["api_key_encrypted"]  # رمزگذاری شده و خام نیست
    assert "sk-test" not in plan["api_key_encrypted"]
    assert plan_service.get_decrypted_api_key(pid) == "sk-test"


def test_no_plan_is_locked(dbs):
    t = tenants_mod.create_tenant("بدون پلن", "nop_plan", "NoPlanPass1!")
    locked, reason = plan_service.check_lock(t["tenant_id"])
    assert locked and reason == "no_plan"


def test_assign_reset_history_and_lock_cycle(dbs, tenant):
    tid = tenant["tenant_id"]
    pid = plan_service.create_plan(
        "سقف ۲ معرفی", plan_service.MODE_WEAK, introduction_quota=2, created_by="tester",
    )
    plan_service.assign_plan(tid, pid, "tester")
    locked, _ = plan_service.check_lock(tid)
    assert not locked

    plan_service.increment_introductions(tid)
    plan_service.increment_introductions(tid)
    locked, reason = plan_service.check_lock(tid)
    assert locked and reason == "intro_quota"

    # تغییر/تخصیص مجدد = ریست (Q33)
    plan_service.assign_plan(tid, pid, "tester")
    locked, _ = plan_service.check_lock(tid)
    assert not locked
    state = plan_service.get_tenant_plan_state(tid)
    assert state["introductions_used"] == 0

    # تاریخچه ثبت شده
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    rows = conn.execute(
        "SELECT plan_id, previous_plan_id, introductions_used_at_change FROM plan_history "
        "WHERE tenant_id=? ORDER BY id",
        (tid,),
    ).fetchall()
    conn.close()
    assert len(rows) >= 2
    assert rows[1][1] == pid  # previous_plan_id در تخصیص دوم
    assert rows[1][2] == 2    # مصرف قبل از ریست در تاریخچه ماند


def test_weak_ignores_message_quota(dbs, tenant):
    tid = tenant["tenant_id"]
    pid = plan_service.create_plan(
        "ضعیف سقف صفر پیام", plan_service.MODE_WEAK,
        message_quota=0, introduction_quota=-1,
    )
    plan_service.assign_plan(tid, pid, "tester")
    plan_service.increment_messages(tid)  # weak = no-op
    locked, reason = plan_service.check_lock(tid)
    assert not locked  # سقف پیام برای weak اعمال نمی‌شود (Q21)
    state = plan_service.get_tenant_plan_state(tid)
    assert state["messages_used"] == 0


def test_medium_message_quota_locks(dbs, tenant):
    tid = tenant["tenant_id"]
    pid = plan_service.create_plan(
        "متوسط ۱ پیام", plan_service.MODE_MEDIUM,
        model="m", base_url="https://x.example/v1", api_key="k",
        message_quota=1, introduction_quota=-1,
    )
    plan_service.assign_plan(tid, pid, "tester")
    plan_service.increment_messages(tid)
    locked, reason = plan_service.check_lock(tid)
    assert locked and reason == "message_quota"


def test_reset_quotas_manual(dbs, tenant):
    tid = tenant["tenant_id"]
    pid = plan_service.create_plan(
        "برای ریست", plan_service.MODE_WEAK, introduction_quota=1,
    )
    plan_service.assign_plan(tid, pid, "tester")
    assert plan_service.increment_introductions(tid) is True
    assert plan_service.check_lock(tid)[0]
    # سقف پر: مصرف اتمیک دوباره False برمی‌گرداند و شمارنده رد نمی‌شود
    assert plan_service.increment_introductions(tid) is False
    state = plan_service.get_tenant_plan_state(tid)
    assert state["introductions_used"] == 1  # نه ۲
    plan_service.reset_quotas(tid, "tester")
    assert not plan_service.check_lock(tid)[0]


def test_lock_message_default_and_custom(dbs, tenant):
    tid = tenant["tenant_id"]
    assert plan_service.lock_message_for(tid) == plan_service.DEFAULT_LOCK_MESSAGE
    plan_service.set_lock_message(tid, "ظرفیت تکمیل شد؛ تماس بگیرید.", "tester")
    assert plan_service.lock_message_for(tid) == "ظرفیت تکمیل شد؛ تماس بگیرید."


def test_audit_log_written(dbs, tenant):
    pid = plan_service.create_plan("پلن ممیزی", plan_service.MODE_WEAK, created_by="tester")
    plan_service.assign_plan(tenant["tenant_id"], pid, "tester")
    from app import audit_service
    actions = [a["action"] for a in audit_service.recent(50)]
    assert "plan.create" in actions
    assert "plan.assign" in actions


def test_llm_test_connection_bad_url():
    ok, msg = llm_service.test_connection("http://127.0.0.1:9/v1", "sk-x", "some-model")
    assert not ok
    assert msg.startswith("❌")


def test_usage_overview_contains_tenant(dbs, tenant):
    pid = plan_service.create_plan("نمای کلی", plan_service.MODE_WEAK, introduction_quota=10)
    plan_service.assign_plan(tenant["tenant_id"], pid, "tester")
    usage = plan_service.get_usage_overview()
    assert tenant["tenant_id"] in usage
    assert usage[tenant["tenant_id"]]["plan_name"] == "نمای کلی"


def test_chat_session_locked_without_plan(dbs):
    """endpoint ویجت: بدون پلن → locked + متن قفل (Q19/Q40)"""
    from fastapi.testclient import TestClient
    from app.main import app
    suffix = uuid.uuid4().hex[:8]
    created = tenants_mod.create_tenant(f"قفل‌شده {suffix}", f"lock_{suffix}", "LockPass12!")
    client = TestClient(app)
    resp = client.post("/api/chat/session", json={"api_key": created["api_key"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["locked"] is True
    assert data["lock_message"]
    assert data["token"] is None

    # بعد از تخصیص پلن، سشن عادی برمی‌گردد
    pid = plan_service.create_plan("بازکننده", plan_service.MODE_WEAK, introduction_quota=5)
    plan_service.assign_plan(created["tenant_id"], pid, "tester")
    resp = client.post("/api/chat/session", json={"api_key": created["api_key"]})
    data = resp.json()
    assert not data.get("locked")
    assert data["token"]


def test_super_admin_pages_render(dbs):
    """ورود سوپرادمین + رندر صفحات پلن/مشتریان"""
    import os
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    username = os.environ.get("SUPER_ADMIN_USERNAME", "superadmin")
    password = os.environ.get("SUPER_ADMIN_PASSWORD", "test_super_pass")
    r = client.post(
        "/admin/login",
        data={"username": username, "password": password},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    r2 = client.get("/admin/super/plans")
    assert r2.status_code == 200
    assert "پلن" in r2.text
    r3 = client.get("/admin")
    assert r3.status_code == 200
    r4 = client.get("/admin/super/plans/new")
    assert r4.status_code == 200


def test_tenant_has_mode_gates_learning_and_extra_admins(dbs, tenant):
    """یادگیری از بازخورد و پنل چندنفره فقط روی پلن‌های advanced (استاندارد/حرفه‌ای)"""
    tid = tenant["tenant_id"]
    assert plan_service.tenant_has_mode(tid, plan_service.MODE_ADVANCED) is False  # بدون پلن

    weak = plan_service.create_plan("پلن ضعیف گیت", plan_service.MODE_WEAK, introduction_quota=50)
    plan_service.assign_plan(tid, weak, "tester")
    assert plan_service.tenant_has_mode(tid, plan_service.MODE_WEAK) is True
    assert plan_service.tenant_has_mode(tid, plan_service.MODE_ADVANCED) is False

    med = plan_service.create_plan(
        "پلن متوسط گیت", plan_service.MODE_MEDIUM, model="m",
        base_url="https://api.example.com/v1", api_key="sk-med", introduction_quota=50,
    )
    plan_service.assign_plan(tid, med, "tester")
    assert plan_service.tenant_has_mode(tid, plan_service.MODE_ADVANCED) is False, \
        "متوسط (medium) نباید یادگیری/پنل چندنفره داشته باشد (طبق جدول قیمت)"

    adv = plan_service.create_plan(
        "پلن پیشرفته گیت", plan_service.MODE_ADVANCED, model="gpt-x",
        base_url="https://api.example.com/v1", api_key="sk-gate", introduction_quota=50,
    )
    plan_service.assign_plan(tid, adv, "tester")
    assert plan_service.tenant_has_mode(tid, plan_service.MODE_ADVANCED) is True


def _super_client():
    """TestClient واردشده به‌عنوان سوپرادمین"""
    import os
    from fastapi.testclient import TestClient
    from app.main import app
    # سطل rate-limit ورود بین همهٔ تست‌ها مشترک است (۱۰ تلاش/IP)؛ مثل
    # test_tenant_llm و test_topups قبل از ورود خالی‌اش می‌کنیم تا
    # ورودهای این تست، بودجهٔ تست‌های بعدی را نخورد.
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute("DELETE FROM rate_limits WHERE key LIKE 'login:%'")
        conn.commit()
    finally:
        conn.close()
    client = TestClient(app)
    r = client.post(
        "/admin/login",
        data={"username": os.environ.get("SUPER_ADMIN_USERNAME", "superadmin"),
              "password": os.environ.get("SUPER_ADMIN_PASSWORD", "test_super_pass")},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return client


def _plan_form_data(plan, **overrides):
    data = {
        "name": plan["name"], "mode": plan["mode"], "model": plan["model"],
        "base_url": plan["base_url"], "api_key": "",
        "message_quota": str(plan["message_quota"]),
        "introduction_quota": str(plan["introduction_quota"]),
        "tier": plan["tier"],
    }
    data.update(overrides)
    return data


def test_panel_edit_plan_limits_end_to_end(dbs, tenant):
    """سوپرادمین از پنل سقف‌های پلن را عوض می‌کند؛ ذخیره و اجرا هر دو همان را می‌بینند"""
    pid = plan_service.create_plan(
        "پیشرفته قابل ویرایش از پنل", plan_service.MODE_ADVANCED, model="gpt-x",
        base_url="https://api.example.com/v1", api_key="sk-edit",
        message_quota=1500, introduction_quota=80, created_by="tester",
    )
    client = _super_client()

    # فرم ویرایش، سقف‌های فعلی را نشان می‌دهد
    r = client.get(f"/admin/super/plans/{pid}/edit")
    assert r.status_code == 200
    assert 'value="1500"' in r.text and 'value="80"' in r.text

    plan = plan_service.get_plan(pid)
    r = client.post(
        f"/admin/super/plans/{pid}",
        data=_plan_form_data(plan, message_quota="3", introduction_quota="2"),
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text
    plan = plan_service.get_plan(pid)
    assert plan["message_quota"] == 3
    assert plan["introduction_quota"] == 2

    # اجرا (enforcement) همان مقادیر ذخیره‌شده را می‌خواند — بدون تغییر کد
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, pid, "tester")
    assert plan_service.increment_messages(tid) is True
    assert plan_service.increment_messages(tid) is True
    assert plan_service.increment_messages(tid) is True
    locked, reason = plan_service.check_lock(tid)
    assert locked and reason == "message_quota"
    plan_service.reset_quotas(tid, "tester")
    assert plan_service.increment_introductions(tid) is True
    assert plan_service.increment_introductions(tid) is True
    assert plan_service.increment_introductions(tid) is False  # سقف جدید: ۲

    # برگرداندن مقادیر اصلی از همان مسیر پنل
    plan = plan_service.get_plan(pid)
    r = client.post(
        f"/admin/super/plans/{pid}",
        data=_plan_form_data(plan, message_quota="1500", introduction_quota="80"),
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text
    plan = plan_service.get_plan(pid)
    assert plan["message_quota"] == 1500
    assert plan["introduction_quota"] == 80


def test_panel_plan_quota_unlimited_and_validation(dbs):
    """خالی در فرم = نامحدود؛ کمتر از ‎-1‎ و عددِ غول‌پیکر خطای تمیز می‌دهند، نه ۵۰۰"""
    pid = plan_service.create_plan(
        "پلن اعتبارسنجی پنل", plan_service.MODE_ADVANCED, model="gpt-x",
        base_url="https://api.example.com/v1", api_key="sk-val",
        message_quota=100, introduction_quota=10, created_by="tester",
    )
    client = _super_client()
    plan = plan_service.get_plan(pid)

    r = client.post(
        f"/admin/super/plans/{pid}",
        data=_plan_form_data(plan, message_quota="", introduction_quota=""),
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text
    plan = plan_service.get_plan(pid)
    assert plan["message_quota"] == plan_service.UNLIMITED
    assert plan["introduction_quota"] == plan_service.UNLIMITED

    r = client.post(
        f"/admin/super/plans/{pid}",
        data=_plan_form_data(plan, message_quota="-5"),
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text
    assert "کمتر از" in r.text
    assert plan_service.get_plan(pid)["message_quota"] == plan_service.UNLIMITED

    r = client.post(
        f"/admin/super/plans/{pid}",
        data=_plan_form_data(plan, message_quota="99999999999999999999999999"),
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text
    assert "بیش از حد بزرگ" in r.text
    assert plan_service.get_plan(pid)["message_quota"] == plan_service.UNLIMITED


def test_plan_quota_service_bounds(dbs):
    with pytest.raises(ValueError):
        plan_service.create_plan("بد", plan_service.MODE_WEAK, message_quota=-2)
    with pytest.raises(ValueError):
        plan_service.create_plan("بد", plan_service.MODE_WEAK,
                                 introduction_quota=10 ** 25)
    pid = plan_service.create_plan("مرزی", plan_service.MODE_WEAK, introduction_quota=5)
    plan = plan_service.get_plan(pid)
    with pytest.raises(ValueError):
        plan_service.update_plan(
            pid, plan["name"], plan["mode"],
            message_quota=plan_service.MAX_INT + 1,
            introduction_quota=plan["introduction_quota"],
        )
    # دقیقاً روی مرز مجاز است
    plan_service.update_plan(
        pid, plan["name"], plan["mode"],
        message_quota=plan_service.MAX_INT,
        introduction_quota=plan["introduction_quota"],
    )
    assert plan_service.get_plan(pid)["message_quota"] == plan_service.MAX_INT


def test_plan_update_audit_records_quota_change(dbs):
    pid = plan_service.create_plan(
        "پلن ممیزی سقف", plan_service.MODE_WEAK,
        message_quota=-1, introduction_quota=10, created_by="tester",
    )
    plan = plan_service.get_plan(pid)
    plan_service.update_plan(
        pid, plan["name"], plan["mode"],
        message_quota=-1, introduction_quota=20, updated_by="tester",
    )
    from app import audit_service
    entries = [a for a in audit_service.recent(100)
               if a["action"] == "plan.update" and a["details"].get("plan_id") == pid]
    assert entries, "plan.update در لاگ ممیزی نیست"
    d = entries[0]["details"]
    assert d["introduction_quota"] == 20
    assert d["previous_introduction_quota"] == 10


def test_add_admin_blocked_on_weak_plan(dbs, tenant):
    """افزودن ادمین دوم (پنل چندنفره) روی پلن ضعیف رد می‌شود"""
    import os
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)

    weak = plan_service.create_plan("پلن ضعیف چندنفره", plan_service.MODE_WEAK, introduction_quota=50)
    plan_service.assign_plan(tenant["tenant_id"], weak, "tester")

    r = client.post(
        "/admin/login",
        data={"username": os.environ.get("SUPER_ADMIN_USERNAME", "superadmin"),
              "password": os.environ.get("SUPER_ADMIN_PASSWORD", "test_super_pass")},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    blocked = client.post(
        f"/admin/super/tenants/{tenant['tenant_id']}/add-admin",
        data={"admin_username": "extra_admin", "admin_password": "AnotherPass123"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert "فقط در پلن‌های استاندارد و حرفه‌ای" in blocked.text

    # متوسط (medium) هم نباید پنل چندنفره داشته باشد
    med = plan_service.create_plan(
        "پلن متوسط چندنفره", plan_service.MODE_MEDIUM, model="m",
        base_url="https://api.example.com/v1", api_key="sk-m", introduction_quota=50,
    )
    plan_service.assign_plan(tenant["tenant_id"], med, "tester")
    blocked2 = client.post(
        f"/admin/super/tenants/{tenant['tenant_id']}/add-admin",
        data={"admin_username": "extra_admin", "admin_password": "AnotherPass123"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert "فقط در پلن‌های استاندارد و حرفه‌ای" in blocked2.text
