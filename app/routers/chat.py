# -*- coding: utf-8 -*-
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from app import session_store as ss
from app import tenants as tenants_mod
from app.db import get_conn
from app.config import USER_RECORDS_DB, MAX_MESSAGE_LENGTH

router = APIRouter(prefix="/api/chat", tags=["chat"])

# ── Rate limit: 30 درخواست در دقیقه به‌ازای IP ──
_RATE_LIMIT = 30
_RATE_WINDOW = 60
_RATE_MAX_KEYS = 10000
_hits: dict[str, deque] = defaultdict(deque)
_LAST_GC = 0.0


def _rate_ok(key: str) -> bool:
    global _LAST_GC
    now = time.time()
    if now - _LAST_GC > _RATE_WINDOW:
        stale = [k for k, dq in _hits.items() if not dq or now - dq[-1] > _RATE_WINDOW]
        for k in stale:
            _hits.pop(k, None)
        _LAST_GC = now
    dq = _hits[key]
    while dq and now - dq[0] > _RATE_WINDOW:
        dq.popleft()
    if not dq:
        _hits.pop(key, None)
        dq = _hits[key]
    if len(dq) >= _RATE_LIMIT:
        return False
    dq.append(now)
    if len(_hits) > _RATE_MAX_KEYS:
        oldest = min(_hits, key=lambda k: _hits[k][0] if _hits[k] else now)
        _hits.pop(oldest, None)
    return True


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


def _resolve_tenant(api_key: str, request: Request) -> dict:
    tenant = tenants_mod.get_tenant_by_key(api_key or "")
    if not tenant:
        raise HTTPException(status_code=401, detail="کلید API نامعتبر است.")
    # محدودیت دامنه (اختیاری)
    domains = [d.strip().lower() for d in tenant["allowed_domains"].split(",") if d.strip()]
    if domains:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        host = urlparse(origin.lower()).hostname or ""
        if host and not _host_allowed(host, domains):
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


@router.post("/session")
async def create_or_restore_session(req: SessionReq, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    rl_key = f"{client_ip}:session"
    if not _rate_ok(rl_key):
        raise HTTPException(status_code=429, detail="درخواست بیش از حد مجاز. لطفاً کمی صبر کنید.")

    tenant = _resolve_tenant(req.api_key, request)
    announcement = _get_announcement(tenant["id"])

    token = req.token
    if token:
        session = ss.get_session(token)
        if session and session["tenant_id"] == tenant["id"]:
            prompt = ss._state_prompt(session)
            return {
                "token": token, "state": session["state"],
                "messages": [{"role": "bot", "text": prompt.text, "html": prompt.html}],
                "quick_replies": prompt.quick_replies,
                "input_type": prompt.input_type,
                "announcement": announcement,
            }

    # سشن جدید
    welcome = tenant["flow_config"].get("welcome") or "سلام! خوش آمدید."
    token = ss.create_session(tenant["id"])
    ss.log_message(token, "bot", ss.md_to_text(welcome))
    return {
        "token": token, "state": 0,
        "messages": [{"role": "bot", "text": welcome, "html": ss.md_to_html(welcome)}],
        "quick_replies": [],
        "input_type": "text",
        "announcement": announcement,
    }


@router.post("/message")
async def post_message(req: MessageReq, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    rl_key = f"{client_ip}:{req.token[:12]}"
    if not _rate_ok(rl_key):
        raise HTTPException(status_code=429, detail="درخواست بیش از حد مجاز. لطفاً کمی صبر کنید.")

    try:
        reply = ss.handle_message(req.token, req.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found or expired")
    except ss.TenantDisabled:
        raise HTTPException(status_code=403, detail="سرویس این مرکز موقتاً غیرفعال است.")

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
