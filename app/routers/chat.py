# -*- coding: utf-8 -*-
import threading
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from app import session_store as ss
from app import tenants as tenants_mod
from app import plan_service
from app.db import get_conn
from app.config import USER_RECORDS_DB, MAX_MESSAGE_LENGTH
from app.ratelimit import client_ip, rate_ok

router = APIRouter(prefix="/api/chat", tags=["chat"])

# ── Rate limit: 30 درخواست در دقیقه به‌ازای IP (دیتابیس‌محور، XFF-aware) ──
_RATE_LIMIT = 60
_RATE_WINDOW = 60

# قفلِ نشست: دو پیام همزمانِ یک توکن نباید session را موازی بخوانند/بنویسند
# (race خواندن-نوشتن → پاسخ دوبل یا state جابه‌جا). striping روی hash توکن —
# ۶۴ قفل کافی است و قفلِ سراسری بی‌جهت همهٔ مستأجرها را معطل می‌کند.
_SESSION_LOCKS = [threading.Lock() for _ in range(64)]


def _session_lock(token: str) -> threading.Lock:
    return _SESSION_LOCKS[hash(token) & 63]


class SessionReq(BaseModel):
    api_key: str = Field(max_length=128)
    token: Optional[str] = Field(default=None, max_length=64)


class MessageReq(BaseModel):
    token: str = Field(max_length=64)
    text: str = Field(max_length=MAX_MESSAGE_LENGTH + 500)


def _host_allowed(host: str, domains: list[str]) -> bool:
    """تطابق دقیق هاست یا ساب‌دامین آن (نه زیررشته‌ای)"""
    host = host.strip().lower().rstrip(".")
    for d in domains:
        d = d.strip().lower().rstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _widget_host(request: Request) -> str:
    """هاستِ خودِ ویجت (همان که /widget را سرو می‌کند).

    ویجت در iframeای بارگذاری می‌شود که از همین هاست می‌آید و درخواست‌هایش
    هم نسبی‌اند، پس Originِ مرورگر همیشه همین است — نه سایتِ مشتری."""
    return (request.headers.get("host") or "").strip().lower().rstrip(".")


def _resolve_tenant(api_key: str, request: Request) -> dict:
    tenant = tenants_mod.get_tenant_by_key(api_key or "")
    if not tenant:
        raise HTTPException(status_code=401, detail="کلید API نامعتبر است.")
    # محدودیت دامنه (اختیاری) — fail-closed: بدون Origin/Referer رد می‌شود
    domains = [d.strip().lower() for d in tenant["allowed_domains"].split(",") if d.strip()]
    if domains:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        host = urlparse(origin.lower()).hostname or ""
        if not host:
            raise HTTPException(status_code=403, detail="دامنه مجاز نیست.")
        # درخواستِ خودِ ویجت از iframe می‌آید و چون fetch‌ها نسبی‌اند، Originش
        # هاستِ خودِ ماست نه سایتِ مشتری. پیش از این استثنا، همین باعث می‌شد
        # ویجتِ هر مشتریِ دامنه‌دار همیشه ۴۰۳ بگیرد و کاملاً از کار بیفتد.
        if host != _widget_host(request) and not _host_allowed(host, domains):
            raise HTTPException(status_code=403, detail="دامنه مجاز نیست.")
    return tenant


def _get_announcement(tenant_id: int) -> str | None:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT text FROM announcements WHERE tenant_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


# def (نه async): SQLite و موتور مچ sync هستند — در threadpool اجرا می‌شوند تا event loop بلاک نشود
@router.post("/session")
def create_or_restore_session(req: SessionReq, request: Request):
    rl_key = f"{client_ip(request)}:session"
    if not rate_ok(rl_key, _RATE_LIMIT, _RATE_WINDOW):
        raise HTTPException(status_code=429, detail="درخواست بیش از حد مجاز. لطفاً کمی صبر کنید.")

    tenant = _resolve_tenant(req.api_key, request)

    # قفل پلن/سقف (Q19/Q40): از لحظه باز شدن ویجت به کاربر نمایش داده می‌شود
    locked, _reason = plan_service.check_lock(tenant["id"])
    if locked:
        return {
            "locked": True,
            "lock_message": plan_service.lock_message_for(tenant["id"]),
            "token": None, "state": -2,
            "messages": [], "quick_replies": [], "input_type": "text",
            "announcement": None,
        }

    announcement = _get_announcement(tenant["id"])

    token = req.token
    if token:
        session = ss.get_session(token)
        if session and session["tenant_id"] == tenant["id"]:
            prompt = ss._state_prompt(session)
            if session["state"] == 0:
                # خوش‌آمد + سؤال اول در یک پیام (همان ساخت سشن)
                messages = [{"role": "bot", "text": prompt.text, "html": prompt.html}]
            else:
                # بازگشتن به سشنِ میانه‌کار: فقط خوش‌آمد + سؤالِ جاری —
                # سابقهٔ گفتگو لود نمی‌شود (تصمیم محصول)
                welcome = tenant["flow_config"].get("welcome") or "سلام! خوش آمدید."
                messages = [
                    {"role": "bot", "text": welcome, "html": ss.md_to_html(welcome)},
                    {"role": "bot", "text": prompt.text, "html": prompt.html},
                ]
            return {
                "token": token, "state": session["state"],
                "messages": messages,
                "quick_replies": prompt.quick_replies,
                "input_type": prompt.input_type,
                "announcement": announcement,
            }

    # سشن جدید — خوش‌آمد + اولین سؤال فلو (یک لاگ، چون meteringِ لحن اینجا نیست)
    token = ss.create_session(tenant["id"])
    prompt = ss.opening_reply(tenant["flow_config"])
    ss.log_message(token, "bot", ss.md_to_text(prompt.text))
    return {
        "token": token, "state": 0,
        "messages": [{"role": "bot", "text": prompt.text, "html": prompt.html}],
        "quick_replies": prompt.quick_replies,
        "input_type": prompt.input_type,
        "announcement": announcement,
    }


@router.post("/message")
def post_message(req: MessageReq, request: Request):
    rl_key = f"{client_ip(request)}:{req.token[:12]}"
    if not rate_ok(rl_key, _RATE_LIMIT, _RATE_WINDOW):
        raise HTTPException(status_code=429, detail="درخواست بیش از حد مجاز. لطفاً کمی صبر کنید.")

    session = ss.get_session(req.token)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found or expired")
    tenant_id = session["tenant_id"]

    # قفل وسط چت (سقف رسیده) — اجرای پیام متوقف می‌شود
    locked, _reason = plan_service.check_lock(tenant_id)
    if locked:
        return {
            "token": req.token, "state": session["state"],
            "locked": True,
            "lock_message": plan_service.lock_message_for(tenant_id),
            "reply": {"text": "", "html": "", "quick_replies": [], "input_type": "text"},
            "done": True,
        }

    over_quota = False
    try:
        with _session_lock(req.token):
            reply = ss.handle_message(req.token, req.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found or expired")
    except ss.TenantDisabled:
        raise HTTPException(status_code=403, detail="سرویس این مرکز موقتاً غیرفعال است.")
    finally:
        # شمارش «پیام هوشمند»: فقط وقتی AI در این نوبت دخالت کرده باشد
        # (لحن/مچینگ/تفسیر — خطا هم شمرده می‌شود Q43؛ weak/بدون‌AI no-op)
        #
        # در finally، نه بعد از try: اگر handle_message بعد از یک فراخوانیِ
        # موفق LLM بترکد (خطای دیتابیس، …)، آن نوبت برای ما هزینه داشته و
        # مشتری باید بابتش محدودیتش را از دست بدهد. بیرون از finally،
        # هر خطای گذرا یعنی پیام‌های رایگانِ نامحدود.
        if plan_service.take_ai_participation():
            # اگر مصرف ناموفق بود، پاسخ نباید به مشتری برسد: check_lock
            # بالا زمانِ خودِ این درخواست را دیده و گذشته، ولی بین آن و این
            # مصرف، درخواستِ همزمانِ دیگری می‌تواند آخرین واحدِ سقف را برده
            # باشد. دور انداختنِ بازگشتی یعنی یک پیامِ AI رایگانِ تمام‌بها.
            over_quota = not plan_service.increment_messages(tenant_id)

    if over_quota:
        # tenant_id از همان سشن اولیه می‌آید — سشن ممکن است بین این دو
        # منقضی شده باشد و get_session اینجا None بدهد (قبلاً TypeError بود)
        return {
            "token": req.token,
            "state": session["state"],
            "locked": True,
            "lock_message": plan_service.lock_message_for(tenant_id),
            "reply": {"text": "", "html": "", "quick_replies": [], "input_type": "text"},
            "done": True,
        }

    session = ss.get_session(req.token)
    return {
        "token": req.token,
        "state": session["state"] if session else -2,
        "reply": {
            "text": reply.text,
            "html": reply.html,
            "quick_replies": reply.quick_replies,
            "input_type": reply.input_type,
        },
        "done": reply.done,
    }


@router.get("/health")
async def health():
    return {"status": "ok"}
