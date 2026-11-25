# -*- coding: utf-8 -*-
"""پنل سوپر ادمین: نمای کلی مشتریان + CRUD مستأجرها"""
import re

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from app.config import USER_RECORDS_DB
from app.db import get_conn
from app import auth
from app import tenants as tenants_mod
from app import audit_service
from app import plan_service
from app.internal_ai_engine import engine
from app.routers.panel_common import _render, _current_user

router = APIRouter(prefix="/admin", tags=["super"], include_in_schema=False)

# ═══════════ پنل سوپر ادمین ═══════════

def _tenant_overview() -> list:
    conn = get_conn(USER_RECORDS_DB)
    rows = conn.execute("SELECT id, name, slug, api_key, allowed_domains, active, created_at FROM tenants ORDER BY id").fetchall()
    out = []
    for (tid, name, slug, api_key, domains, active, created) in rows:
        leads = conn.execute("SELECT COUNT(*) FROM user_requests WHERE tenant_id=?", (tid,)).fetchone()[0]
        chats = conn.execute("SELECT COUNT(*) FROM widget_sessions WHERE tenant_id=?", (tid,)).fetchone()[0]
        consultants = conn.execute("SELECT COUNT(*) FROM consultants WHERE tenant_id=?", (tid,)).fetchone()[0]
        out.append({
            "id": tid, "name": name, "slug": slug, "api_key": api_key,
            "domains": domains or "", "active": active, "created": created,
            "leads": leads, "chats": chats, "consultants": consultants,
        })
    conn.close()
    return out


async def _super_dashboard(request: Request, user: dict, flash: str | None = None):
    tenants_list = await run_in_threadpool(_tenant_overview)
    from app import plan_service
    plan_map = await run_in_threadpool(plan_service.get_usage_overview)
    plans_list = await run_in_threadpool(plan_service.list_plans, False)

    def _llm_configs() -> dict:
        # هر مشتریِ LLM‌دار: پیکربندی فعلی‌اش (اختصاصی یا ارث‌از-پلن) برای فرم
        return {
            t["id"]: plan_service.get_tenant_llm_config(t["id"])
            for t in tenants_list if plan_map.get(t["id"])
        }

    llm_configs = await run_in_threadpool(_llm_configs)
    return _render(request, "super_tenants.html", {
        "user": user, "tenants": tenants_list, "flash": flash,
        "plan_map": plan_map, "plans_list": plans_list,
        "llm_configs": llm_configs,
        "plan_mode_labels": plan_service.MODE_LABELS,
        "quota_label": plan_service.quota_label,
    })


@router.post("/super/tenants")
async def create_tenant(request: Request,
                        name: str = Form(...), admin_username: str = Form(...),
                        admin_password: str = Form(...), allowed_domains: str = Form("")):
    user = _current_user(request)
    if not user or user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای سوپر ادمین مجاز است.")
    if len(admin_password) < 8:
        return await _super_dashboard(request, user, flash="❌ رمز ادمین باید حداقل ۸ کاراکتر باشد.")
    if len(admin_password) > 128:
        return await _super_dashboard(request, user, flash="❌ رمز ادمین حداکثر ۱۲۸ کاراکتر باشد.")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", admin_username):
        return await _super_dashboard(request, user, flash="❌ نام کاربری: ۳ تا ۳۲ کاراکتر انگلیسی/عدد/نقطه/خط تیره.")
    if not name.strip():
        return await _super_dashboard(request, user, flash="❌ نام مشتری الزامی است.")
    try:
        res = await run_in_threadpool(
            tenants_mod.create_tenant, name.strip()[:80], admin_username, admin_password, allowed_domains[:500]
        )
        engine.set_tenant(res["tenant_id"], force=True)
        audit_service.log(user["username"], "tenant.create",
                          {"tenant_id": res["tenant_id"], "name": name.strip()})
        return await _super_dashboard(request, user, flash=f"✅ مشتری «{name}» ساخته شد — کلید API: {res['api_key']}")
    except Exception as e:
        return await _super_dashboard(request, user, flash=f"❌ خطا در ساخت مشتری: {e}")


@router.post("/super/tenants/{tenant_id}/regenerate-key")
async def regenerate_key(request: Request, tenant_id: int):
    user = _current_user(request)
    if not user or user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای سوپر ادمین مجاز است.")
    new_key = tenants_mod.new_api_key()

    def _save():
        conn = get_conn(USER_RECORDS_DB)
        conn.execute("UPDATE tenants SET api_key=? WHERE id=?", (new_key, tenant_id))
        conn.commit()
        conn.close()

    await run_in_threadpool(_save)
    audit_service.log(user["username"], "tenant.regenerate_key", {"tenant_id": tenant_id})
    return await _super_dashboard(request, user, flash=f"🔑 کلید API بازتولید شد: {new_key} (کلید قبلی باطل شد)")


@router.post("/super/tenants/{tenant_id}/reset-password")
async def reset_admin_password(request: Request, tenant_id: int, new_password: str = Form(...)):
    user = _current_user(request)
    if not user or user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای سوپر ادمین مجاز است.")
    if len(new_password) < 8:
        return await _super_dashboard(request, user, flash="❌ رمز باید حداقل ۸ کاراکتر باشد.")
    if len(new_password) > 128:
        return await _super_dashboard(request, user, flash="❌ رمز حداکثر ۱۲۸ کاراکتر باشد.")

    def _save():
        conn = get_conn(USER_RECORDS_DB)
        cur = conn.execute(
            "UPDATE panel_users SET password_hash=? WHERE tenant_id=? AND role='admin'",
            (auth.hash_password(new_password), tenant_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount

    updated = await run_in_threadpool(_save)
    if not updated:
        return await _super_dashboard(request, user, flash="❌ ادمینی برای این مشتری یافت نشد.")
    audit_service.log(user["username"], "tenant.reset_password", {"tenant_id": tenant_id})
    return await _super_dashboard(request, user, flash="✅ رمز ادمین(های) این مشتری ریست شد.")


@router.post("/super/tenants/{tenant_id}/toggle")
async def toggle_tenant(request: Request, tenant_id: int):
    user = _current_user(request)
    if not user or user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای سوپر ادمین مجاز است.")

    def _toggle():
        conn = get_conn(USER_RECORDS_DB)
        cur = conn.execute("UPDATE tenants SET active = 1 - active WHERE id=?", (tenant_id,))
        conn.commit()
        conn.close()

    await run_in_threadpool(_toggle)
    audit_service.log(user["username"], "tenant.toggle", {"tenant_id": tenant_id})
    return await _super_dashboard(request, user, flash="وضعیت مشتری تغییر کرد.")


@router.post("/super/tenants/{tenant_id}/domains")
async def update_domains(request: Request, tenant_id: int, allowed_domains: str = Form("")):
    user = _current_user(request)
    if not user or user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای سوپر ادمین مجاز است.")

    def _save():
        conn = get_conn(USER_RECORDS_DB)
        conn.execute("UPDATE tenants SET allowed_domains=? WHERE id=?", (allowed_domains.strip(), tenant_id))
        conn.commit()
        conn.close()

    await run_in_threadpool(_save)
    audit_service.log(user["username"], "tenant.domains",
                      {"tenant_id": tenant_id, "domains": allowed_domains.strip()[:200]})
    return await _super_dashboard(request, user, flash="✅ دامنه‌های مجاز به‌روزرسانی شد.")


@router.post("/super/tenants/{tenant_id}/add-admin")
async def add_admin(request: Request, tenant_id: int,
                    admin_username: str = Form(...), admin_password: str = Form(...)):
    user = _current_user(request)
    if not user or user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای سوپر ادمین مجاز است.")
    if len(admin_password) < 8:
        return await _super_dashboard(request, user, flash="❌ رمز باید حداقل ۸ کاراکتر باشد.")
    if len(admin_password) > 128:
        return await _super_dashboard(request, user, flash="❌ رمز حداکثر ۱۲۸ کاراکتر باشد.")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", admin_username):
        return await _super_dashboard(request, user, flash="❌ نام کاربری نامعتبر است.")
    tenant = await run_in_threadpool(tenants_mod.get_tenant, tenant_id)
    if not tenant:
        return await _super_dashboard(request, user, flash="❌ مشتری یافت نشد.")
    if not await run_in_threadpool(
        plan_service.tenant_has_mode, tenant_id, plan_service.MODE_ADVANCED
    ):
        return await _super_dashboard(
            request, user, flash="❌ پنل چندنفره فقط در پلن‌های استاندارد و حرفه‌ای فعال است.",
        )

    def _save():
        conn = get_conn(USER_RECORDS_DB)
        try:
            conn.execute(
                "INSERT INTO panel_users (username, password_hash, role, tenant_id, active) VALUES (?,?,?,?,1)",
                (admin_username, auth.hash_password(admin_password), "admin", tenant_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        await run_in_threadpool(_save)
        audit_service.log(user["username"], "tenant.add_admin",
                          {"tenant_id": tenant_id, "admin_username": admin_username})
        return await _super_dashboard(request, user, flash=f"✅ ادمین «{admin_username}» به مشتری اضافه شد.")
    except Exception:
        return await _super_dashboard(request, user, flash="❌ این نام کاربری قبلاً استفاده شده است.")
