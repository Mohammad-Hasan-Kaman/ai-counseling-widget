# -*- coding: utf-8 -*-
"""تست‌های شمارندهٔ «پیام هوشمند» — فقط نوبت‌هایی که AI در آن دخالت کرده شمرده می‌شوند."""
import uuid

import pytest

from app import plan_service
from app import tenants as tenants_mod


@pytest.fixture()
def tenant(dbs):
    suffix = uuid.uuid4().hex[:8]
    return tenants_mod.create_tenant(
        f"مرکز شمارنده {suffix}", f"cnt_admin_{suffix}", "CntPass123!"
    )


def _plan(name, mode, msg_quota, intro=50):
    if mode == plan_service.MODE_WEAK:
        return plan_service.create_plan(name, mode, message_quota=msg_quota,
                                        introduction_quota=intro, created_by="tester")
    return plan_service.create_plan(
        name, mode, model="m", base_url="https://api.example.com/v1",
        api_key="sk-test", message_quota=msg_quota, introduction_quota=intro,
        created_by="tester",
    )


def test_take_is_read_once(dbs, tenant):
    """take مصرف‌شده است: بار دوم False برمی‌گرداند (نشت بین requestها رخ نمی‌دهد)"""
    assert plan_service.take_ai_participation() is False
    plan_service.mark_ai_participation()
    assert plan_service.take_ai_participation() is True
    assert plan_service.take_ai_participation() is False


def test_weak_plan_never_consumes(dbs, tenant):
    """پلن پایه (بدون AI): حتی با mark، مصرف صفر می‌ماند و ویجت قفل نمی‌شود"""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(tid, _plan("پایه", plan_service.MODE_WEAK, 5), "tester")
    for _ in range(3):
        plan_service.mark_ai_participation()
        if plan_service.take_ai_participation():
            plan_service.increment_messages(tid)
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 0
    assert plan_service.check_lock(tid)[0] is False


def test_medium_counts_only_when_ai_participated(dbs, tenant):
    tid = tenant["tenant_id"]
    plan_service.assign_plan(
        tid, _plan("استاندارد", plan_service.MODE_MEDIUM, 500), "tester")

    # پیام بدون دخالت AI (خوش‌آمد، رد شماره، GHQ) → شمرده نمی‌شود
    assert plan_service.take_ai_participation() is False
    # پیام با دخالت AI → یک واحد
    plan_service.mark_ai_participation()
    if plan_service.take_ai_participation():
        plan_service.increment_messages(tid)
    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 1


def test_llm_call_marks_participation(dbs, tenant, monkeypatch):
    """فراخوانی واقعی از لایهٔ فلو علامت می‌زند (rewrite_tone)"""
    from app import llm_flow
    cfg = {"mode": "medium", "base_url": "https://x.example/v1",
           "api_key": "k", "model": "m"}
    monkeypatch.setattr(llm_flow.llm_service, "chat_completion",
                        lambda *a, **k: "متن بازنویسی‌شدهٔ طبیعی و صمیمی برای مراجع")
    plan_service.take_ai_participation()  # صفر اولیه
    out = llm_flow.rewrite_tone(cfg, "این یک متن بلند فارسی است که حتماً بیش از چهل کاراکتر دارد تا لحن‌بخشی انجام شود.")
    assert out == "متن بازنویسی‌شدهٔ طبیعی و صمیمی برای مراجع"
    assert plan_service.take_ai_participation() is True


def test_short_text_skips_llm_entirely(dbs, tenant, monkeypatch):
    """متن کوتاه به LLM نمی‌رود → نه علامت، نه مصرف"""
    from app import llm_flow
    called = []
    monkeypatch.setattr(llm_flow.llm_service, "chat_completion",
                        lambda *a, **k: called.append(1))
    plan_service.take_ai_participation()
    assert llm_flow.rewrite_tone({"model": "m", "base_url": "u", "api_key": "k"},
                                 "سلام") == "سلام"
    assert called == []
    assert plan_service.take_ai_participation() is False


def test_test_connection_never_marks(dbs, tenant):
    """تست اتصال از سوپر ادمین نباید «پیام هوشمند» مصرف کند"""
    from app import llm_service
    plan_service.take_ai_participation()
    llm_service.test_connection("http://127.0.0.1:9/v1", "sk-x", "m")
    assert plan_service.take_ai_participation() is False


def test_end_to_end_weak_chat_consumes_zero(dbs):
    """چت واقعی با پلن پایه: چند پیام → شمارنده صفر، سقف پیام قفل نشود"""
    from fastapi.testclient import TestClient
    from app.main import app
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(f"پایه چت {suffix}", f"base_{suffix}", "BasePass123!")
    tid = t["tenant_id"]
    plan_service.assign_plan(tid, _plan("پایه چت", plan_service.MODE_WEAK, 5), "tester")

    client = TestClient(app)
    r = client.post("/api/chat/session", json={"api_key": t["api_key"]})
    data = r.json()
    assert not data.get("locked"), data
    token = data["token"]
    for text in ["سلام", "خیلی استرس دارم", "می‌خواهم مشاور", "09120000000", "باشه"]:
        resp = client.post("/api/chat/message", json={"token": token, "text": text})
        assert resp.status_code == 200, resp.text
        assert resp.json().get("locked") is not True, "پلن پایه نباید قفل شود"

    assert plan_service.get_tenant_plan_state(tid)["messages_used"] == 0


def test_over_quota_reply_is_replaced_by_lock(dbs, tenant, monkeypatch):
    """اگر مصرف شکست بخورد، پاسخِ AI نباید به مشتری برسد.

    check_lock و increment_messages دو تراکنش جدا هستند؛ اگر بینشان درخواستِ
    همزمانِ دیگری آخرین واحدِ سقف را بردارد، مصرفِ این نوبت ناموفق است. آن
    وقت پاسخِ تولیدشده (که هزینهٔ LLM داشته) نباید به مشتری نشان داده شود."""
    from fastapi.testclient import TestClient
    from app.main import app
    tid = tenant["tenant_id"]
    # سقفِ یک‌واحدی: مصرفِ این نوبت قطعاً ناموفق می‌شود.
    plan_service.assign_plan(tid, _plan("استاندارد", plan_service.MODE_MEDIUM, 1), "tester")
    assert plan_service.increment_messages(tid) is True  # آخرین واحد

    # check_lock را باز نگه می‌داریم (همان وضعیتِ مسابقه: واحد را درخواستِ
    # همزمانِ دیگری برده، ولی این درخواست پیش‌تر رد شده) و مصرف را قطعی
    # ناموفق می‌کنیم.
    monkeypatch.setattr(plan_service, "check_lock", lambda t: (False, ""), raising=True)
    monkeypatch.setattr(plan_service, "mark_ai_participation", lambda: None)
    monkeypatch.setattr(plan_service, "take_ai_participation", lambda: True)

    client = TestClient(app)
    r = client.post("/api/chat/session", json={"api_key": tenant["api_key"]})
    token = r.json()["token"]
    resp = client.post("/api/chat/message", json={"token": token, "text": "سلام"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("locked") is True, body
    assert body["done"] is True, body
    assert body["reply"]["text"] == "", body
    assert body.get("lock_message"), body
