# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
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
from app.routers import chat, admin

TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


async def _crawl_job():
    try:
        await run_in_threadpool(crawl_available_slots)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("crawl job error: %s", e)


async def _gc_job():
    await run_in_threadpool(gc_expired_sessions)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(init_all_dbs)
    sched = AsyncIOScheduler()
    if CRAWLER_ENABLED:
        sched.add_job(_crawl_job, "interval", hours=CRAWLER_INTERVAL_HOURS, id="crawl")
        _initial_crawl = asyncio.create_task(_crawl_job())  # کراول اولیه (معادل post_init)
        app.state.initial_crawl = _initial_crawl
    sched.add_job(_gc_job, "interval", minutes=60, id="gc_sessions")
    sched.start()
    yield
    sched.shutdown(wait=False)
    task = getattr(app.state, "initial_crawl", None)
    if task and not task.done():
        task.cancel()


app = FastAPI(title="Nikravan AI Chat Widget", lifespan=lifespan)

@app.middleware("http")
async def security_headers(request, call_next):
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
    # عنوان/نام مرکز از کلید API (چندمشتری — نه هاردکد نیک‌روان)
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
