# -*- coding: utf-8 -*-
"""مدیریت پلن/سقف — سوپرادمین: CRUD الگوها، تخصیص، ریست سقف، تست اتصال LLM
+ نمایش پلن برای ادمین tenant و متن قفل per-tenant"""
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from urllib.parse import quote

from app.config import BASE_DIR
from app import auth
from app import plan_service
from app import audit_service
from app import llm_service

router = APIRouter(prefix="/admin", tags=["plans"], include_in_schema=False)
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    return TEMPLATES.TemplateResponse(request, template, ctx, status_code=status_code)


def _super(request: Request) -> dict:
    return auth.require_super(request)


def _parse_quota(raw: str) -> int:
    raw = (raw or "").strip()
    if raw == "":
        return plan_service.UNLIMITED
    try:
        v = int(raw)
    except ValueError:
        raise ValueError("سقف باید عدد صحیح باشد (-1 = نامحدود).")
    if v < -1:
        raise ValueError("سقف نمی‌تواند کمتر از -1 باشد.")
    return v


def _plans_page(request: Request, user: dict, flash: str | None = None):
    plans = plan_service.list_plans()
    usage = plan_service.get_usage_overview()
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM tenants").fetchall()}
    conn.close()
    usage_rows = []
    for tid in sorted(usage.keys()):
        u = usage[tid]
        row = {"tenant_id": tid, "tenant_name": names.get(tid, f"#{tid}"), "plan": u is not None}
        if u:
            row.update(u)
        usage_rows.append(row)
    return _render(request, "super_plans.html", {
        "user": user, "plans": plans, "usage_rows": usage_rows,
        "mode_labels": plan_service.MODE_LABELS, "flash": flash,
        "audit": audit_service.recent(50),
    })


def _plan_form(request: Request, user: dict, plan: dict | None,
               flash: str | None = None, test_result: tuple | None = None):
    return _render(request, "super_plan_form.html", {
        "user": user, "plan": plan, "mode_labels": plan_service.MODE_LABELS,
        "tier_labels": plan_service.TIER_LABELS,
        "flash": flash, "test_result": test_result,
    })


@router.get("/super/plans", response_class=HTMLResponse)
async def plans_list(request: Request):
    user = _super(request)
    return await run_in_threadpool(_plans_page, request, user, request.query_params.get("flash"))


@router.get("/super/plans/new", response_class=HTMLResponse)
async def plan_new(request: Request):
    user = _super(request)
    return _plan_form(request, user, None)


@router.post("/super/plans")
async def plan_create(request: Request,
                      name: str = Form(...), mode: str = Form(...),
                      model: str = Form(""), base_url: str = Form(""),
                      api_key: str = Form(""),
                      message_quota: str = Form("-1"),
                      introduction_quota: str = Form("-1"),
                      tier: str = Form("")):
    user = _super(request)
    try:
        mq = _parse_quota(message_quota)
        iq = _parse_quota(introduction_quota)
        await run_in_threadpool(
            plan_service.create_plan, name, mode, model, base_url, api_key, mq, iq,
            user["username"], tier,
        )
    except ValueError as e:
        return _plan_form(request, user, None, flash=f"❌ {e}")
    return await run_in_threadpool(
        _plans_page, request, user, f"✅ پلن «{name.strip()}» ساخته شد."
    )


@router.get("/super/plans/{plan_id}/edit", response_class=HTMLResponse)
async def plan_edit(request: Request, plan_id: int):
    user = _super(request)
    plan = await run_in_threadpool(plan_service.get_plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="پلن یافت نشد.")
    return _plan_form(request, user, plan)


@router.post("/super/plans/test-connection")
async def plan_test_connection(request: Request,
                               base_url: str = Form(...), api_key: str = Form(""),
                               model: str = Form(...), plan_id: str = Form("")):
    user = _super(request)
    key = (api_key or "").strip()
    plan = None
    if plan_id:
        plan = await run_in_threadpool(plan_service.get_plan, int(plan_id))
    if not key and plan:
        # کلید خالی در فرم ویرایش = استفاده از کلید ذخیره‌شده
        try:
            key = await run_in_threadpool(plan_service.get_decrypted_api_key, plan["id"])
        except Exception:
            key = ""
    ok, msg = await run_in_threadpool(
        llm_service.test_connection, (base_url or "").strip(), key, (model or "").strip()
    )
    audit_service.log(user["username"], "plan.test_connection",
                      {"base_url": (base_url or "").strip(), "model": (model or "").strip(), "ok": ok})
    return _plan_form(request, user, plan, test_result=(ok, msg))


@router.post("/super/plans/{plan_id}")
async def plan_update(request: Request, plan_id: int,
                      name: str = Form(...), mode: str = Form(...),
                      model: str = Form(""), base_url: str = Form(""),
                      api_key: str = Form(""),
                      message_quota: str = Form("-1"),
                      introduction_quota: str = Form("-1"),
                      tier: str = Form("")):
    user = _super(request)
    plan = await run_in_threadpool(plan_service.get_plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="پلن یافت نشد.")
    try:
        mq = _parse_quota(message_quota)
        iq = _parse_quota(introduction_quota)
        await run_in_threadpool(
            plan_service.update_plan, plan_id, name, mode, model, base_url,
            api_key, mq, iq, user["username"], tier,
        )
    except ValueError as e:
        return _plan_form(request, user, plan, flash=f"❌ {e}")
    return await run_in_threadpool(
        _plans_page, request, user, f"✅ پلن «{name.strip()}» ذخیره شد."
    )


@router.post("/super/tenants/{tenant_id}/assign-plan")
async def tenant_assign_plan(request: Request, tenant_id: int, plan_id: int = Form(...)):
    user = _super(request)
    from app.routers.super_routes import _super_dashboard
    try:
        await run_in_threadpool(plan_service.assign_plan, tenant_id, plan_id, user["username"])
    except ValueError as e:
        return await _super_dashboard(request, user, flash=f"❌ {e}")
    plan = await run_in_threadpool(plan_service.get_plan, plan_id)
    return await _super_dashboard(
        request, user,
        flash=f"✅ پلن «{plan['name']}» به این مشتری تخصیص یافت — شمارنده‌ها ریست شدند.",
    )


@router.post("/super/tenants/{tenant_id}/reset-quotas")
async def tenant_reset_quotas(request: Request, tenant_id: int):
    user = _super(request)
    from app.routers.super_routes import _super_dashboard
    await run_in_threadpool(plan_service.reset_quotas, tenant_id, user["username"])
    return await _super_dashboard(request, user, flash="🔄 شمارنده‌های پیام و معرفی ریست شدند.")


@router.post("/super/tenants/{tenant_id}/quota-override")
async def tenant_quota_override(request: Request, tenant_id: int,
                                message_quota: str = Form(""),
                                introduction_quota: str = Form("")):
    """سقف اختصاصیِ همین مشتری — خالی = برگشت به سقف پلن"""
    user = _super(request)
    from app.routers.super_routes import _super_dashboard
    try:
        await run_in_threadpool(
            plan_service.set_quota_override, tenant_id, message_quota, introduction_quota,
            user["username"],
        )
    except ValueError as e:
        return await _super_dashboard(request, user, flash=f"❌ {e}")
    return await _super_dashboard(
        request, user, flash="✅ سقف اختصاصی این مشتری ذخیره شد (فقط همین مشتری).")


# ── پیکربندی LLM اختصاصیِ هر مشتری ──

def _resolve_llm_key(tenant_id: int, api_key: str) -> str:
    """کلیدِ موردِ استفاده در تست/دریافت مدل‌ها، از همان سه حالتِ فرم:
    تازه (کلیدِ تایپ‌شده) ← ذخیره‌شدهٔ مشتری ← ارث‌از-پلن. خالی = هیچ‌کدام."""
    raw = (api_key or "").strip()
    if raw:
        return raw
    try:
        own = plan_service.get_tenant_api_key(tenant_id)
    except Exception:
        own = ""
    if own:
        return own
    state = plan_service.get_tenant_plan_state(tenant_id)
    if not state:
        return ""
    try:
        return plan_service.get_decrypted_api_key(state["plan_id"])
    except Exception:
        return ""


@router.post("/super/tenants/{tenant_id}/llm")
async def tenant_set_llm(request: Request, tenant_id: int,
                         base_url: str = Form(""),
                         model: str = Form(""),
                         api_key: str = Form("")):
    """درگاه/کلید/مدلِ همین مشتری — فقط همین مشتری را تغییر می‌دهد"""
    user = _super(request)
    from app.routers.super_routes import _super_dashboard
    try:
        await run_in_threadpool(
            plan_service.set_tenant_llm, tenant_id, base_url, model, api_key,
            user["username"],
        )
    except ValueError as e:
        return await _super_dashboard(request, user, flash=f"❌ {e}")
    return await _super_dashboard(
        request, user,
        flash="✅ پیکربندی LLM این مشتری ذخیره شد (فقط همین مشتری).",
    )


@router.post("/super/tenants/{tenant_id}/llm/models")
async def tenant_llm_models(request: Request, tenant_id: int,
                            base_url: str = Form(""),
                            api_key: str = Form("")):
    """مدل‌های زندهٔ همین درگاه — JSON برای datalist فرمِ سوپرادمین"""
    user = _super(request)
    cfg = await run_in_threadpool(plan_service.get_tenant_llm_config, tenant_id)
    url = (base_url or "").strip() or cfg["base_url"] or cfg["plan_base_url"]
    if not url:
        return JSONResponse({"ok": False, "models": [],
                             "error": "base_url خالی است — اول درگاه را وارد کنید."})
    key = await run_in_threadpool(_resolve_llm_key, tenant_id, api_key)
    if not key:
        return JSONResponse({"ok": False, "models": [],
                             "error": "API key خالی است — اول کلید را وارد کنید."})
    models, err = await run_in_threadpool(llm_service.list_models, url, key)
    if err:
        return JSONResponse({"ok": False, "models": [], "error": err})
    audit_service.log(user["username"], "tenant.llm_models",
                      {"tenant_id": tenant_id, "base_url": url, "count": len(models)})
    return JSONResponse({"ok": True, "models": models, "error": ""})


@router.post("/super/tenants/{tenant_id}/llm/test")
async def tenant_llm_test(request: Request, tenant_id: int,
                          base_url: str = Form(""),
                          model: str = Form(""),
                          api_key: str = Form("")):
    """تست اتصالِ پیکربندیِ همین مشتری — الگوی plan_test_connection"""
    user = _super(request)
    from app.routers.super_routes import _super_dashboard
    cfg = await run_in_threadpool(plan_service.get_tenant_llm_config, tenant_id)
    url = (base_url or "").strip() or cfg["base_url"] or cfg["plan_base_url"]
    mdl = (model or "").strip() or cfg["model"] or cfg["plan_model"]
    key = await run_in_threadpool(_resolve_llm_key, tenant_id, api_key)
    if not (url and mdl and key):
        flash = "❌ base_url، مدل و API key هر سه لازم است."
        return await _super_dashboard(request, user, flash=flash)
    ok, msg = await run_in_threadpool(llm_service.test_connection, url, key, mdl)
    audit_service.log(user["username"], "tenant.llm_test",
                      {"tenant_id": tenant_id, "base_url": url, "model": mdl, "ok": ok})
    return await _super_dashboard(
        request, user,
        flash=("✅ " if ok else "❌ ") + msg.lstrip("✅❌ "),
    )


@router.post("/super/tenants/{tenant_id}/renew-quotas")
async def tenant_renew_quotas(request: Request, tenant_id: int):
    """تمدید سقف‌های ماهانه — شمارنده صفر، سقف اختصاصی و پلن دست‌نخورده"""
    user = _super(request)
    from app.routers.super_routes import _super_dashboard
    try:
        await run_in_threadpool(plan_service.renew_quotas, tenant_id, user["username"])
    except ValueError as e:
        return await _super_dashboard(request, user, flash=f"❌ {e}")
    return await _super_dashboard(
        request, user,
        flash="♻️ سقف‌های پیام و معرفی این مشتری تمدید شد (شمارنده‌ها صفر شدند).",
    )


# ── بستهٔ اضافه (top-up) ──

@router.get("/super/topups", response_class=HTMLResponse)
async def topups_page(request: Request):
    user = _super(request)
    from app.config import USER_RECORDS_DB
    from app.db import get_conn

    def _names() -> dict:
        conn = get_conn(USER_RECORDS_DB)
        rows = conn.execute("SELECT id, name FROM tenants").fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}

    packages = await run_in_threadpool(plan_service.list_packages, True)
    pending = await run_in_threadpool(plan_service.list_topup_requests, None, "pending")
    history = await run_in_threadpool(plan_service.list_topup_requests, None, "")
    names = await run_in_threadpool(_names)
    for r in pending + history:
        r["tenant_name"] = names.get(r["tenant_id"], f"#{r['tenant_id']}")
    return _render(request, "super_topups.html", {
        "user": user, "packages": packages, "pending": pending,
        "history": [r for r in history if r["status"] != "pending"][:40],
        "price_label": plan_service.rial_label,
        "flash": request.query_params.get("flash"),
    })


@router.post("/super/topups/packages")
async def topup_package_create(request: Request,
                               name: str = Form(...),
                               extra_messages: str = Form("0"),
                               extra_introductions: str = Form("0"),
                               price_rials: str = Form("0")):
    user = _super(request)
    try:
        pkg_id = await run_in_threadpool(
            plan_service.create_package, name, int(extra_messages or 0),
            int(extra_introductions or 0), int(price_rials or 0), user["username"],
        )
        flash = f"✅ بستهٔ «{name.strip()}» ساخته شد."
    except (ValueError, TypeError) as e:
        flash = f"❌ {e}"
    return RedirectResponse(f"/admin/super/topups?flash={quote(flash)}", status_code=303)


@router.post("/super/topups/packages/{package_id}/toggle")
async def topup_package_toggle(request: Request, package_id: int):
    user = _super(request)
    conn_packages = await run_in_threadpool(plan_service.list_packages, True)
    current = next((p for p in conn_packages if p["id"] == package_id), None)
    if current is None:
        return RedirectResponse("/admin/super/topups?flash=❌ بسته یافت نشد.", status_code=303)
    await run_in_threadpool(
        plan_service.set_package_active, package_id, not current["active"], user["username"])
    flash = "🟢 بسته فعال شد." if not current["active"] else "⚪️ بسته غیرفعال شد."
    return RedirectResponse(f"/admin/super/topups?flash={quote(flash)}", status_code=303)


@router.post("/super/topups/{request_id}/resolve")
async def topup_resolve(request: Request, request_id: int, approve: str = Form("")):
    user = _super(request)
    ok = approve == "1"
    try:
        tid, msg, intro = await run_in_threadpool(
            plan_service.resolve_topup, request_id, ok, user["username"])
    except ValueError as e:
        return RedirectResponse(
            f"/admin/super/topups?flash={quote('❌ ' + str(e))}", status_code=303)
    flash = (f"✅ بستهٔ «{msg} پیام هوشمند + {intro} معرفی» به اعتبار مشتری #{tid} اضافه شد."
             if ok else "🚫 درخواست رد شد.")
    return RedirectResponse(f"/admin/super/topups?flash={quote(flash)}", status_code=303)


@router.post("/lock-message")
async def save_lock_message(request: Request, lock_message: str = Form("")):
    """سوپرادمین یا ادمین همان tenant (Q47)"""
    user = auth.require_admin(request)
    if not user["tenant_id"]:
        # سوپرادمین tenant ندارد (tenant_id=NULL) — UPDATE روی id=NULL صفر سطر
        # می‌خورد و بی‌صدا «موفق» گزارش می‌شد. این فرم فقط برای ادمینِ همان مرکز است.
        raise HTTPException(status_code=403, detail="متن قفل فقط از پنل همان مرکز قابل تنظیم است.")
    await run_in_threadpool(
        plan_service.set_lock_message, user["tenant_id"], lock_message, user["username"]
    )
    return RedirectResponse("/admin", status_code=303)


# ── نمونه‌های few-shot برای پرامپت LLM — مدیریت سوپر ادمین (Q48 ج) ──

@router.get("/super/examples", response_class=HTMLResponse)
async def examples_list(request: Request):
    user = _super(request)
    from app.config import USER_RECORDS_DB
    from app.db import get_conn

    def _load():
        conn = get_conn(USER_RECORDS_DB)
        rows = conn.execute("""
            SELECT e.id, e.tenant_id, t.name, e.prompt_text, e.corrected_text,
                   e.status, e.created_by, e.created_at
            FROM few_shot_examples e LEFT JOIN tenants t ON t.id = e.tenant_id
            ORDER BY e.id DESC LIMIT 300
        """).fetchall()
        conn.close()
        return [{
            "id": r[0], "tenant_id": r[1], "tenant_name": r[2] or "—",
            "prompt": r[3], "corrected": r[4], "status": r[5],
            "created_by": r[6], "created_at": r[7],
        } for r in rows]

    examples = await run_in_threadpool(_load)
    return _render(request, "super_examples.html", {
        "user": user, "examples": examples,
        "flash": request.query_params.get("flash"),
    })


@router.post("/super/examples/{example_id}/status")
async def example_set_status(request: Request, example_id: int,
                             status: str = Form(...)):
    user = _super(request)
    if status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="وضعیت نامعتبر.")
    from app.config import USER_RECORDS_DB
    from app.db import get_conn

    def _upd():
        conn = get_conn(USER_RECORDS_DB)
        conn.execute(
            "UPDATE few_shot_examples SET status=?, reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, user["username"], example_id),
        )
        conn.commit()
        conn.close()

    await run_in_threadpool(_upd)
    import app.audit_service as _audit
    _audit.log(user["username"], "example.status",
               {"example_id": example_id, "status": status})
    label = "تأیید" if status == "approved" else "رد"
    return RedirectResponse(f"/admin/super/examples?flash=✅ نمونه {label} شد.", status_code=303)


@router.post("/super/examples/{example_id}/delete")
async def example_delete(request: Request, example_id: int):
    user = _super(request)
    from app.config import USER_RECORDS_DB
    from app.db import get_conn

    def _del():
        conn = get_conn(USER_RECORDS_DB)
        conn.execute("DELETE FROM few_shot_examples WHERE id=?", (example_id,))
        conn.commit()
        conn.close()

    await run_in_threadpool(_del)
    import app.audit_service as _audit
    _audit.log(user["username"], "example.delete", {"example_id": example_id})
    return RedirectResponse("/admin/super/examples?flash=🗑 نمونه حذف شد.", status_code=303)
