# -*- coding: utf-8 -*-
"""تست‌های شمارش در مسیر خطای چت.

اگر `ss.handle_message` بعد از یک فراخوانیِ موفق LLM استثنا بدهد (خطای DB،
قطع اتصال، …)، نباید مصرف از مشتری بیفتد: آن فراخوانی برای صاحبش هزینه دارد
و مشتری باید بابتش محدودیتش را از دست بدهد — وگرنه هر خطای گذرا یعنی
پیام‌های رایگانِ نامحدود.
"""
import uuid

import pytest

from app import plan_service
from app import tenants as tenants_mod


@pytest.fixture()
def advanced_tenant(dbs):
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(
        f"مرکز خطا {suffix}", f"err_{suffix}", "ErrPass123!")
    tid = t["tenant_id"]
    plan_service.assign_plan(tid, plan_service.create_plan(
        f"استاندارد {suffix}", plan_service.MODE_MEDIUM,
        model="m", base_url="https://api.example.com/v1", api_key="sk-test",
        message_quota=500, introduction_quota=50, created_by="tester"), "tester")
    return t


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _fake_llm(monkeypatch):
    """LLM را «موفق» می‌کنیم تا واقعاً علامت بخورد و هزینه داشته باشد.

    شمارنده را برمی‌گرداند تا تست بتواندAssert کند فراخوانی واقعاً رخ داده."""
    from app import llm_service
    seen = []
    monkeypatch.setattr(
        llm_service, "chat_completion",
        lambda *a, **k: seen.append(1) or
        "متنِ بازنویسی‌شدهٔ طبیعی و صمیمی برای مراجعه‌کننده به مرکز")
    return seen


def test_billed_llm_charge_survives_a_crash_afterwards(
        dbs, advanced_tenant, monkeypatch):
    """استثنای غیر-LLMError بعد از LLM موفق نباید مصرف را ببلعد.

    `_llm` پیش از فراخوانی علامت می‌زند، پس حتی اگر `handle_message` بعدش
    بترکد، آن نوبت برای مشتری هزینه داشته و باید شمرده شود."""
    from app import session_store as ss

    tid = advanced_tenant["tenant_id"]
    llm_calls = _fake_llm(monkeypatch)

    # نقطهٔ واقعیِ ترکیدن: نوشتن پاسخ ربات، درست بعد از فراخوانی LLM
    real_log = ss.log_message
    calls = {"n": 0}

    def flaky_log(token, role, text):
        calls["n"] += 1
        if role == "bot" and calls["n"] > 1:
            raise RuntimeError("دیتابیس لحظه‌ای در دسترس نیست")
        return real_log(token, role, text)

    monkeypatch.setattr(ss, "log_message", flaky_log)

    before = plan_service.get_tenant_plan_state(tid)["messages_used"]
    client = _client()
    r = client.post("/api/chat/session", json={"api_key": advanced_tenant["api_key"]})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    resp = client.post("/api/chat/message", json={"token": token, "text": "سلام"})

    # باید واقعاً از مسیرِ استثنا عبور کرده باشیم، وگرنه تست بی‌معناست
    assert resp.status_code == 500, f"ترکیدن رخ نداد: {resp.status_code} {resp.text[:200]}"
    assert len(llm_calls) == 1, (
        f"LLM صدا زده نشد ({len(llm_calls)} بار) — تست چیزِ دیگری را می‌سنجد")

    after = plan_service.get_tenant_plan_state(tid)["messages_used"]
    assert after == before + 1, (
        f"فراخوانی LLM انجام شد و مشتری بابتش محدودیتش را از دست نداد "
        f"({before} → {after}) — یعنی هزینهٔ LLM می‌سوزد و مصرف ثبت نمی‌شود"
    )


def test_crash_does_not_lock_the_tenant_out(dbs, advanced_tenant, monkeypatch):
    """خطای موقت نباید مشتری را بی‌دلیل قفل کند یا سشنش را ناموجود کند"""
    from app import session_store as ss

    _fake_llm(monkeypatch)
    real_log = ss.log_message
    calls = {"n": 0}

    def flaky_log(token, role, text):
        calls["n"] += 1
        if role == "bot" and calls["n"] > 1:
            raise RuntimeError("boom")
        return real_log(token, role, text)

    monkeypatch.setattr(ss, "log_message", flaky_log)
    client = _client()

    r = client.post("/api/chat/session", json={"api_key": advanced_tenant["api_key"]})
    token = r.json()["token"]
    assert client.post("/api/chat/message",
                       json={"token": token, "text": "سلام"}).status_code == 500

    # سشن باید هنوز قابل استفاده باشد — پیام بعدی نباید ۴۰۴ بدهد
    resp = client.post("/api/chat/message", json={"token": token, "text": "ادامه"})
    assert resp.status_code in (200, 500), (
        f"سشن بعد از خطا مرد: {resp.status_code} {resp.text[:200]}")
    assert resp.status_code != 404, "سشن باید بعد از خطای موقت باقی بماند"
