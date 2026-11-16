# -*- coding: utf-8 -*-
"""مدیریت شعبه‌ها از پنل ادمین + حفظ شرط relationship در save_flow

منبع حقیقت: گزینه‌های استپ branch در flow_config — افزودن/تغییر/حذف از صفحه
/admin/branches باید بلافاصل در فلو (و در نتیجه چیپ‌های چت و موتور مچینگ) بنشیند."""
import json
import uuid

import pytest

from app import tenants as tenants_mod

_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(scope="module")
def panel(dbs):
    """مشتری تازه (فلوی عمومی، بدون استپ شعبه) + ادمینش — فقط یک ورود (سقف rate-limit)"""
    from fastapi.testclient import TestClient
    from app.main import app

    suffix = uuid.uuid4().hex[:6]
    username = f"branch_admin_{suffix}"
    created = tenants_mod.create_tenant(
        f"مرکز شعبه {suffix}", username, "BranchPass123!")
    client = TestClient(app)
    r = client.post(
        "/admin/login",
        data={"username": username, "password": "BranchPass123!"},
        headers=_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return {"client": client, "tenant_id": created["tenant_id"]}


def _flow(tid: int) -> dict:
    return tenants_mod.get_tenant(tid)["flow_config"]


def _branch_step(tid: int) -> dict | None:
    for s in _flow(tid)["steps"]:
        if isinstance(s, dict) and s.get("key") == "branch":
            return s
    return None


def test_branch_crud_from_panel(panel):
    client, tid = panel["client"], panel["tenant_id"]

    # فلوی عمومی استپ شعبه ندارد → صفحه دکمهٔ ساخت نشان می‌دهد
    r = client.get("/admin/branches")
    assert r.status_code == 200
    assert "افزودن سؤال شعبه به فلو" in r.text

    # ساخت استپ
    r = client.post("/admin/branches/create-step", headers=_ORIGIN)
    assert r.status_code == 200
    step = _branch_step(tid)
    assert step is not None
    assert step["options"] == ["اهمیتی ندارد"]
    assert step.get("source") == "branch"

    # افزودن دو شعبه
    r = client.post("/admin/branches", data={"name": "  ونک "}, headers=_ORIGIN)
    assert r.status_code == 200
    assert _branch_step(tid)["options"] == ["ونک", "اهمیتی ندارد"]
    client.post("/admin/branches", data={"name": "جردن"}, headers=_ORIGIN)
    assert _branch_step(tid)["options"] == ["ونک", "جردن", "اهمیتی ندارد"]

    # تکراری ممنوع
    r = client.post("/admin/branches", data={"name": "ونک"}, headers=_ORIGIN)
    assert "قبلاً" in r.text
    assert len(_branch_step(tid)["options"]) == 3

    # «اهمیتی ندارد» قابل ثبت نیست
    r = client.post("/admin/branches", data={"name": "اهمیتی ندارد"}, headers=_ORIGIN)
    assert "ثابت" in r.text
    assert len(_branch_step(tid)["options"]) == 3

    # تغییر نام با ایندیس (بدون احتساب «اهمیتی ندارد»)
    r = client.post("/admin/branches/update",
                    data={"index": "0", "name": "ونک شمالی"}, headers=_ORIGIN)
    assert r.status_code == 200
    assert _branch_step(tid)["options"] == ["ونک شمالی", "جردن", "اهمیتی ندارد"]

    # حذف با ایندیس
    r = client.post("/admin/branches/delete", data={"index": "1"}, headers=_ORIGIN)
    assert r.status_code == 200
    assert _branch_step(tid)["options"] == ["ونک شمالی", "اهمیتی ندارد"]

    # صفحه نام باقی‌مانده را نشان می‌دهد
    r = client.get("/admin/branches")
    assert "ونک شمالی" in r.text
    assert "جردن" not in r.text


def test_save_flow_keeps_relationship_conditional(panel):
    """regression: اعتبارسنج conditional نباید شرط relationship را بیندازد"""
    client, tid = panel["client"], panel["tenant_id"]
    steps = [
        {"key": "relationship", "type": "choice"},   # فرم پنل می‌فرستد؛ حذف+prepend می‌شود
        {"key": "spouse_relation", "type": "choice",
         "conditional": "relationship=برای فرزندم",
         "options": ["خیلی خوب", "میانه", "پایین"],
         "label": "کیفیت ارتباط شما با همسرتان چگونه است؟"},
        {"key": "full_name", "type": "text"},
        {"key": "phone", "type": "phone"},
    ]
    payload = {
        "welcome": "سلام", "ghq": "1", "ending": "recommend",
        "thanks_text": "متشکریم", "for_other": "1",
        "steps_json": json.dumps(steps, ensure_ascii=False),
    }
    r = client.post("/admin/flow", data=payload, headers=_ORIGIN)
    assert r.status_code == 200, r.text
    flow = _flow(tid)
    keys = [s["key"] for s in flow["steps"]]
    assert keys[0] == "relationship"
    assert keys[1] == "spouse_relation"
    assert flow["steps"][1]["conditional"] == "relationship=برای فرزندم"
    assert flow["for_other"] is True

    # بدون «برای دیگری»: شرط هرگز برقرار نمی‌شود → کل استپ حذف (بی‌شرط به همه تحمیل نشود)
    payload["for_other"] = "0"
    r = client.post("/admin/flow", data=payload, headers=_ORIGIN)
    assert r.status_code == 200, r.text
    keys = [s["key"] for s in _flow(tid)["steps"]]
    assert "relationship" not in keys
    assert "spouse_relation" not in keys
    assert keys == ["full_name", "phone"]


def _post_flow(client, steps, for_other="1"):
    payload = {
        "welcome": "سلام", "ghq": "0", "ending": "lead_capture",
        "thanks_text": "متشکریم", "for_other": for_other,
        "steps_json": json.dumps(steps, ensure_ascii=False),
    }
    return client.post("/admin/flow", data=payload, headers=_ORIGIN)


def test_save_flow_hardening(panel):
    """سخت‌سازی save_flow: ورودی غیرdict، فلوی خالی، شرط گم‌شده، source ناقص"""
    client, tid = panel["client"], panel["tenant_id"]

    # ۱) آیتم‌های غیرdict (رشته/عدد) کنار می‌روند —۵۰۰ نمی‌دهند؛ استپ سالم می‌ماند
    r = _post_flow(client, ["junk", 42, {"key": "full_name", "type": "text"}])
    assert r.status_code == 200, r.text
    assert [s["key"] for s in _flow(tid)["steps"]] == ["relationship", "full_name"]

    # ۲) فقط relationship بدون «برای دیگری» → فلوی خالی رد می‌شود؛ فلو قبلی دست‌نخورده می‌ماند
    before = json.loads(json.dumps(_flow(tid)))  # کپی — کش ممکن است همان شیء را برگرداند
    r = _post_flow(client, [{"key": "relationship", "type": "choice"}], for_other="0")
    assert r.status_code == 200, r.text
    assert "نامعتبر" in r.text
    assert _flow(tid)["steps"] == before["steps"]

    # ۳) شرطِ اشاره‌کننده به استپِ غایب → کل استپ حذف (بی‌شرط ذخیره نمی‌شود)
    r = _post_flow(client, [
        {"key": "full_name", "type": "text"},
        {"key": "legacy_q", "type": "text", "conditional": "expectation=خوب"},
    ], for_other="0")
    assert r.status_code == 200, r.text
    assert [s["key"] for s in _flow(tid)["steps"]] == ["full_name"]

    # ۴) prev_therapy بدون source (مثل ادیتور قدیمی) → source استاندارد می‌گیرد
    #    و شرط prev_detail به کمک همان source حفظ می‌شود
    r = _post_flow(client, [
        {"key": "prev_therapy", "type": "choice", "options": ["بله", "خیر"], "label": "سابقه؟"},
        {"key": "prev_detail", "type": "text",
         "conditional": "has_prev_therapy=بله", "label": "توضیح"},
    ], for_other="0")
    assert r.status_code == 200, r.text
    flow = _flow(tid)
    pt = next(s for s in flow["steps"] if s["key"] == "prev_therapy")
    pd = next(s for s in flow["steps"] if s["key"] == "prev_detail")
    assert pt["source"] == "has_prev_therapy"
    assert pd["conditional"] == "has_prev_therapy=بله"
