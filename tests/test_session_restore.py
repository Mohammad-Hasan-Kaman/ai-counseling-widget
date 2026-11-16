# -*- coding: utf-8 -*-
"""بازگردانی سشن باید خوش‌آمد + سؤال جاری را برگرداند — نه سابقهٔ گفتگو.

ویجت هنگام restore پیام‌های قبلی را پاک می‌کند و فقط پاسخ همین درخواست را
رندر می‌کند. تصمیم محصول: سشنِ میانه‌کار هم فقط خوش‌آمد (تا مراجع بداند کجاست)
به‌همراه سؤالِ همین مرحله نشان داده شود؛ ترنسکریپت کامل لود نمی‌شود."""
import uuid

from app import plan_service
from app import tenants as tenants_mod


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _weak_tenant(name_prefix: str, slug_prefix: str) -> dict:
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(
        f"{name_prefix} {suffix}", f"{slug_prefix}_{suffix}", "Restore123!")
    plan_service.assign_plan(
        t["tenant_id"],
        plan_service.create_plan(f"ضعیف {suffix}", plan_service.MODE_WEAK),
        "tester",
    )
    return t


def test_restore_mid_session_shows_welcome_and_current_question(dbs):
    t = _weak_tenant("خوش‌آمد", "wl")
    client = _client()

    r1 = client.post("/api/chat/session", json={"api_key": t["api_key"]})
    data1 = r1.json()
    token = data1["token"]
    assert token
    assert "خوش آمدید" in data1["messages"][0]["text"]

    r2 = client.post("/api/chat/message", json={"token": token, "text": "برای خودم"})
    assert r2.status_code == 200

    # بازگردانی سشن میانه‌کار — خوش‌آمد + سؤال جاری (بدون سابقه)
    r3 = client.post("/api/chat/session", json={"api_key": t["api_key"], "token": token})
    data3 = r3.json()
    assert not data3.get("locked")
    assert data3["state"] == 2
    msgs = data3["messages"]
    # دقیقاً دو پیام: خوش‌آمد و سؤالِ مرحلهٔ جاری
    assert len(msgs) == 2
    assert all(m["role"] == "bot" for m in msgs)
    assert "خوش آمدید" in msgs[0]["text"]
    # سؤالِ جاری (نام) — چیپس از همان state میآید
    assert "نام" in msgs[-1]["text"]
    # سابقهٔ کاربر نباید برگردد
    assert not any(m["role"] == "user" for m in msgs)


def test_restore_state_zero_returns_single_opening_prompt(dbs):
    """سشنِ تازه (state 0) خوش‌آمد+سؤال اولِ ترکیبی را در یک پیام می‌آورد"""
    t = _weak_tenant("حالت‌صفر", "z0")
    client = _client()
    r1 = client.post("/api/chat/session", json={"api_key": t["api_key"]})
    token = r1.json()["token"]

    r2 = client.post("/api/chat/session", json={"api_key": t["api_key"], "token": token})
    data2 = r2.json()
    assert data2["state"] == 0
    msgs = data2["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "bot"
    assert "خوش آمدید" in msgs[0]["text"]
