# -*- coding: utf-8 -*-
"""اشتراکی پنل ادمین: رندر قالب، کاربر جاری، tenant"""
from fastapi import Request, HTTPException
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app import auth
from app import tenants as tenants_mod

TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    # لینک «گزارش دوره‌ای» فقط برای پلن حرفه‌ای و لینک «بستهٔ اضافه» فقط برای
    # مشتریِ دارای پلن — یک جا محاسبه می‌شود تا همهٔ صفحات پنل بدون تکرار کد
    # به‌روز بمانند.
    user = ctx.get("user")
    if user is not None and user.get("role") == "admin":
        from app import plan_service
        tid = user.get("tenant_id")
        if "can_report" not in ctx:
            ctx["can_report"] = plan_service.tenant_has_report(tid)
        if "has_plan" not in ctx:
            ctx["has_plan"] = plan_service.get_tenant_plan_state(tid) is not None
    return TEMPLATES.TemplateResponse(request, template, ctx, status_code=status_code)


def _current_user(request: Request) -> dict | None:
    return auth.get_current_user(request)


def _tenant_of(user: dict) -> dict:
    t = tenants_mod.get_tenant(user["tenant_id"])
    if not t:
        raise HTTPException(status_code=404, detail="مشتری یافت نشد.")
    return t
