# -*- coding: utf-8 -*-
"""گزارش دوره‌ای پنل مشتری — صفحه + خروجی اکسل (فقط پلن «حرفه‌ای»)."""
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from app import plan_service
from app import report_service
from app.admin_tools import export_report_excel
from app.routers.panel_common import _render, _current_user

router = APIRouter(prefix="/admin", tags=["report"], include_in_schema=False)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BLOCKED = "گزارش دوره‌ای فقط در پلن «حرفه‌ای» فعال است."


async def _guard(request: Request):
    """(user, tenant_id, deny). deny = RedirectResponse برای لاگین، یا «blocked»
    وقتی پلن «حرفه‌ای» نباشد — user همیشه برمی‌گردد تا صفحهٔ داشبورد با پیام رندر شود."""
    user = _current_user(request)
    if not user:
        return None, None, RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    if not await run_in_threadpool(plan_service.tenant_has_report, user["tenant_id"]):
        return user, user["tenant_id"], "blocked"
    return user, user["tenant_id"], None


@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request, days: int | None = None):
    user, tenant_id, deny = await _guard(request)
    if isinstance(deny, RedirectResponse):
        return deny
    if deny == "blocked":
        from app.routers.admin import _tenant_dashboard
        return await _tenant_dashboard(request, user, flash=f"❌ {BLOCKED}")

    d = report_service.sanitize_days(days)
    data = await run_in_threadpool(report_service.build_report, tenant_id, d)
    return _render(request, "admin_report.html", {
        "user": user, "rep": data, "can_report": True,
    })


@router.get("/report/export")
async def report_export(request: Request, days: int | None = None):
    _user, tenant_id, deny = await _guard(request)
    if isinstance(deny, RedirectResponse):
        return deny
    if deny == "blocked":
        raise HTTPException(status_code=403, detail=BLOCKED)

    d = report_service.sanitize_days(days)
    data = await run_in_threadpool(export_report_excel, tenant_id, d)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return StreamingResponse(
        iter([data]),
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f"attachment; filename=report_{d}d_{stamp}.xlsx"},
    )
