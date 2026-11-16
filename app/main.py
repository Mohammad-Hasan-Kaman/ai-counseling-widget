# -*- coding: utf-8 -*-
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import (
    BASE_DIR, CORS_ORIGINS, CRAWLER_INTERVAL_HOURS, CRAWLER_ENABLED,
)
from app.db import init_all_dbs, gc_expired_sessions
from app.crawler import crawl_available_slots
from app.routers import chat, admin, plans, inbox_leads, super_routes, report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


async def _crawl_job():
    """کراول زمان‌بندی‌شده — با lease توکن‌دار از اجرای همزمان چند worker یا همپوشانی جلوگیری می‌شود"""
    from app import crawl_state as cs
    from app import tenants as tenants_mod
    token = await run_in_threadpool(cs.try_acquire, cs.CRAWL_LEASE, cs.CRAWL_LEASE_TTL)
    if not token:
        return
    try:
        # نوبت‌های کراول‌شده مالِ tenant نمونه‌اند؛ ستون tenant_id درخواستِ
        # موتور مچ را از نشت بین مراکز جلوگیری می‌کند، پس باید صریح بنشیند
        def _primary_tenant_id():
            from app.db import get_conn
            from app.config import USER_RECORDS_DB
            conn = get_conn(USER_RECORDS_DB)
            try:
                r = conn.execute(
                    "SELECT id FROM tenants WHERE slug=? AND active=1", ("demo",)
                ).fetchone()
                return r[0] if r else 0
            finally:
                conn.close()
        tenant_id = await run_in_threadpool(_primary_tenant_id)
        if not tenant_id:
            # بدون این گارد، کراول حدود ۸۶ ردیف را با tenant_id=0 می‌نوشت که
            # هیچ مشتری‌ای آن‌ها را نمی‌بیند (خواندن با شناسهٔ واقعی فیلتر
            # می‌کند) — یعنی داده‌ای که فقط پاک می‌شود. بهتر است خطا بدهیم.
            logger.error("کراولِ خودکار اجرا نشد: مرکزِ «demo» در tenants پیدا نشد.")
            return

        # شمارنده‌های پیشرفت از همین ابتدا صفر می‌شوند (نمایش درست در پنل)
        await run_in_threadpool(cs.reset_progress)

        def _progress(done: int, total: int, current: str, free_total: int):
            # heartbeat با توکن خودمان + به‌روزرسانی شمارنده‌ها
            try:
                cs.renew(cs.CRAWL_LEASE, token, cs.CRAWL_LEASE_TTL)
                cs.update_progress(done=done, total=total,
                                   current=current, free_total=free_total)
            except Exception:
                pass

        await run_in_threadpool(
            crawl_available_slots, progress_cb=_progress, tenant_id=tenant_id)
        await run_in_threadpool(cs.update_progress, None, None, None, None, None, True)
    except Exception as e:
        logger.error("crawl job error: %s", e)
        try:
            await run_in_threadpool(cs.update_progress, None, None, None, None, str(e)[:200], True)
        except Exception:
            pass
    finally:
        await run_in_threadpool(cs.release, cs.CRAWL_LEASE, token)


async def _gc_job():
    from app import crawl_state as cs
    token = await run_in_threadpool(cs.try_acquire, cs.GC_LEASE, cs.GC_LEASE_TTL)
    if not token:
        return
    try:
        await run_in_threadpool(gc_expired_sessions)
    finally:
        await run_in_threadpool(cs.release, cs.GC_LEASE, token)


async def _quota_reset_job():
    """ریست ماهانهٔ شمارنده‌ها — بدون این، «/ ماه» در جدول قیمت یعنی «برای همیشه»."""
    from app import plan_service
    try:
        reset = await run_in_threadpool(plan_service.reset_expired_quotas)
        if reset:
            logger.info("monthly quota reset: %s tenant(s)", reset)
    except Exception as e:
        logger.error("quota reset job error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(init_all_dbs)
    from app import crawl_state as _cs
    await run_in_threadpool(_cs.ensure_tables)
    sched = AsyncIOScheduler()
    if CRAWLER_ENABLED:
        sched.add_job(_crawl_job, "interval", hours=CRAWLER_INTERVAL_HOURS, id="crawl")
        _initial_crawl = asyncio.create_task(_crawl_job())  # کراول اولیه (معادل post_init)
        app.state.initial_crawl = _initial_crawl
    sched.add_job(_gc_job, "interval", minutes=60, id="gc_sessions")
    sched.add_job(_quota_reset_job, "interval", minutes=60, id="monthly_quota_reset")
    sched.start()
    yield
    sched.shutdown(wait=False)
    task = getattr(app.state, "initial_crawl", None)
    if task and not task.done():
        task.cancel()


app = FastAPI(title="AI Counseling Widget", lifespan=lifespan)

@app.middleware("http")
async def security_headers(request, call_next):
    # CSRF: POSTهای پنل ادمین باید از همین هاست بیایند (Origin یا Referer)
    if request.method == "POST" and request.url.path.startswith("/admin"):
        host = (request.headers.get("host") or "").lower()
        ref = request.headers.get("origin") or request.headers.get("referer") or ""
        netloc = urlparse(ref.lower()).netloc if ref else ""
        if not netloc or netloc != host:
            return JSONResponse(
                status_code=403,
                content={"detail": "درخواست نامعتبر (CSRF check failed)."},
            )
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # /widget باید در iframe سایت‌های مشتری باز شود
    if request.url.path != "/widget":
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    allow_credentials=True,
)

app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(inbox_leads.router)
app.include_router(super_routes.router)
app.include_router(plans.router)
app.include_router(report.router)

# استاتیک با کش کوتاه — تغییرات ویجت سریع به سایت‌های مشتری می‌رسد
class NoCacheStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, max-age=300"
        return resp


app.mount("/static", NoCacheStatic(directory=str(BASE_DIR / "static")), name="static")


@app.get("/widget", response_class=HTMLResponse)
async def widget_page(key: str = ""):
    from html import escape as _escape
    # عنوان/نام مرکز از کلید API (چندمشتری — نه هاردکد مرکز نمونه)
    tenant_name = "سامانه پذیرش"
    if key:
        from app import tenants as _tenants
        t = _tenants.get_tenant_by_key(key)
        if t:
            tenant_name = t["name"]
    html = (BASE_DIR / "static" / "widget.html").read_text(encoding="utf-8")
    html = html.replace("__TENANT_NAME__", _escape(tenant_name))
    return HTMLResponse(html)


@app.get("/demo/{demo_key}", response_class=HTMLResponse)
async def demo_page(demo_key: str):
    """دموی اختصاصی هر مشتری: لینک از پنل مشتری می‌آید و کلید همان مشتری را دارد.
    گفتگوها مثل چت واقعی با کلید همان مشتری ثبت می‌شوند."""
    from app import tenants as _tenants
    from html import escape as _escape
    t = _tenants.get_tenant_by_key(demo_key)
    if not t:
        return HTMLResponse(
            "<!DOCTYPE html><html lang='fa' dir='rtl'><meta charset='utf-8'>"
            "<body style='font-family:Tahoma;text-align:center;padding:60px;'>"
            "❌ دموی موردنظر یافت نشد یا منقضی شده است.</body></html>",
            status_code=404,
        )
    html = (BASE_DIR / "static" / "demo.html").read_text(encoding="utf-8")
    html = html.replace("__API_KEY__", demo_key)
    html = html.replace("__DEMO_NAME__", _escape(t["name"]))
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def root():
    # صفحه خانه (لندینگ)
    html = (BASE_DIR / "static" / "landing.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/pricing", response_class=HTMLResponse)
async def pricing():
    # صفحه قیمت‌گذاری
    html = (BASE_DIR / "static" / "pricing.html").read_text(encoding="utf-8")
    return HTMLResponse(html)
