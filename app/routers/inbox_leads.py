# -*- coding: utf-8 -*-
"""گفتگوها (inbox) و درخواست‌ها (leads) + خروجی اکسل + تعارض — سرچ/سورت/صفحه‌بندی"""
import json
from datetime import datetime

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from app.config import USER_RECORDS_DB
from app.db import get_conn
from app.admin_tools import export_users_excel
from app.routers.panel_common import _render, _current_user, _tenant_of

router = APIRouter(prefix="/admin", tags=["inbox"], include_in_schema=False)

# ── گفتگوها (inbox) — سرچ/سورت/صفحه‌بندی سرور-ساید (Q16/Q31) ──

_INBOX_PER_PAGE = 50
_LEADS_PER_PAGE = 50


def _page(raw, default=1) -> int:
    try:
        p = int(raw or default)
    except (TypeError, ValueError):
        p = default
    return max(1, p)


@router.get("/inbox", response_class=HTMLResponse)
async def inbox(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    tenant_id = user["tenant_id"] if user["role"] == "admin" else None
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    q = (request.query_params.get("q") or "").strip()[:100]
    sort = request.query_params.get("sort") or "updated_desc"
    page = _page(request.query_params.get("page"))
    chats, total = await run_in_threadpool(_list_chats, tenant_id, q, sort, page)
    pages = max(1, -(-total // _INBOX_PER_PAGE))
    return _render(request, "admin_inbox.html", {
        "user": user, "chats": chats, "q": q, "sort": sort,
        "page": page, "pages": pages, "total": total,
    })


def _list_chats(tenant_id: int, q: str = "", sort: str = "updated_desc",
                page: int = 1) -> tuple[list, int]:
    conn = get_conn(USER_RECORDS_DB)
    where = ["s.tenant_id=?"]
    args: list = [tenant_id]
    if q:
        like = f"%{q}%"
        where.append(
            "(s.data LIKE ? OR EXISTS (SELECT 1 FROM widget_messages m "
            "WHERE m.token=s.token AND m.text LIKE ?))"
        )
        args.extend([like, like])
    where_sql = " AND ".join(where)
    order = {
        "updated_desc": "s.updated_at DESC",
        "updated_asc": "s.updated_at ASC",
        "name": "json_extract(s.data, '$.full_name') COLLATE NOCASE ASC",
    }.get(sort, "s.updated_at DESC")
    total = conn.execute(
        f"SELECT COUNT(*) FROM widget_sessions s WHERE {where_sql}", args,
    ).fetchone()[0]
    offset = (max(1, page) - 1) * _INBOX_PER_PAGE
    rows = conn.execute(f"""
        SELECT s.token, s.state, s.data, s.created_at, s.updated_at,
               (s.expires_at < datetime('now')) AS expired,
               (SELECT text FROM widget_messages m WHERE m.token=s.token ORDER BY m.id DESC LIMIT 1) AS last_msg,
               (SELECT COUNT(*) FROM widget_messages m WHERE m.token=s.token) AS msg_count
        FROM widget_sessions s WHERE {where_sql}
        ORDER BY {order} LIMIT ? OFFSET ?
    """, (*args, _INBOX_PER_PAGE, offset)).fetchall()
    conn.close()
    chats = []
    for (token, state, data_json, created, updated, expired, last_msg, msg_count) in rows:
        data = json.loads(data_json or "{}")
        state_label = {
            -2: "✅ تکمیل‌شده", -3: "📋 GHQ",
            -1: "📋 GHQ",
        }.get(state, f"⏳ مرحله {state + 1}")
        rel = data.get("relationship") or ""
        chats.append({
            "token": token, "name": data.get("full_name") or "بازدیدکننده",
            "phone": data.get("phone", ""), "state_label": state_label,
            "created": created, "updated": updated, "last_msg": (last_msg or "")[:60],
            "msg_count": msg_count,
            "relationship": rel, "initiator": data.get("initiator_name") or "",
            "expired": bool(expired),
        })
    return chats, total


@router.get("/inbox/{token}", response_class=HTMLResponse)
async def inbox_detail(request: Request, token: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]
    conn = get_conn(USER_RECORDS_DB)
    sess = conn.execute(
        "SELECT state, data, created_at, updated_at FROM widget_sessions WHERE token=? AND tenant_id=?",
        (token, tenant_id),
    ).fetchone()
    if not sess:
        conn.close()
        raise HTTPException(status_code=404, detail="گفتگو یافت نشد.")
    msgs = conn.execute(
        "SELECT role, text, created_at FROM widget_messages WHERE token=? ORDER BY id",
        (token,),
    ).fetchall()
    data = json.loads(sess[1] or "{}")
    conn.close()
    return _render(request, "admin_inbox_detail.html", {
        "user": user, "token": token, "msgs": msgs,
        "lead": data, "created": sess[2], "updated": sess[3],
    })


# ── درخواست‌ها — سرچ/سورت/صفحه‌بندی سرور-ساید (Q16/Q31) ──

@router.get("/leads", response_class=HTMLResponse)
async def leads(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    q = (request.query_params.get("q") or "").strip()[:100]
    sort = request.query_params.get("sort") or "created_desc"
    page = _page(request.query_params.get("page"))
    rows, total = await run_in_threadpool(_list_leads, user["tenant_id"], q, sort, page)
    pages = max(1, -(-total // _LEADS_PER_PAGE))
    return _render(request, "admin_leads.html", {
        "user": user, "rows": rows, "q": q, "sort": sort,
        "page": page, "pages": pages, "total": total,
    })


def _list_leads(tenant_id: int, q: str = "", sort: str = "created_desc",
                page: int = 1) -> tuple[list, int]:
    conn = get_conn(USER_RECORDS_DB)
    where = ["tenant_id=?"]
    args: list = [tenant_id]
    if q:
        like = f"%{q}%"
        where.append("(full_name LIKE ? OR phone LIKE ? OR topic LIKE ? OR custom_data LIKE ?)")
        args.extend([like, like, like, like])
    where_sql = " AND ".join(where)
    order = {
        "created_desc": "created_at DESC",
        "created_asc": "created_at ASC",
        "name": "full_name COLLATE NOCASE ASC",
        "age": "age ASC",
    }.get(sort, "created_at DESC")
    total = conn.execute(
        f"SELECT COUNT(*) FROM user_requests WHERE {where_sql}", args,
    ).fetchone()[0]
    offset = (max(1, page) - 1) * _LEADS_PER_PAGE
    rows = conn.execute(f"""
        SELECT id, full_name, phone, gender, age, topic, ghq_total, request_number,
               created_at, recommendations, custom_data
        FROM user_requests WHERE {where_sql}
        ORDER BY {order} LIMIT ? OFFSET ?
    """, (*args, _LEADS_PER_PAGE, offset)).fetchall()
    conn.close()
    out = []
    for (rid, name, phone, gender, age, topic, ghq, rn, created, recs_json, custom_json) in rows:
        rec_names = ""
        try:
            recs = json.loads(recs_json) if recs_json else []
            rec_names = "، ".join(r.get("name", "") for r in recs if isinstance(r, dict))
        except Exception:
            pass
        custom = {}
        try:
            custom = json.loads(custom_json) if custom_json else {}
        except Exception:
            pass
        out.append({"id": rid, "name": name, "phone": phone, "gender": gender,
                    "age": age, "topic": topic, "ghq": ghq, "rn": rn,
                    "created": created, "recs": rec_names,
                    "relationship": custom.get("relationship", ""),
                    "initiator": custom.get("initiator_name", "")})
    return out, total


@router.get("/export")
async def export(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    data = await run_in_threadpool(export_users_excel, user["tenant_id"])
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=leads_{stamp}.xlsx"},
    )


# ── تعارض شماره‌ها (فقط tenant نمونه و همهٔ مشتری‌ها — per-tenant) ──

@router.get("/conflicts", response_class=HTMLResponse)
async def conflicts_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    from app import user_db
    conflicts = await run_in_threadpool(user_db.list_phone_conflicts, user["tenant_id"])
    suspicious = await run_in_threadpool(user_db.get_suspicious_phones, user["tenant_id"])
    tenant = _tenant_of(user)
    flash = "✅ تعارض رسیدگی شد." if request.query_params.get("ok") else None
    return _render(request, "admin_conflicts.html", {
        "user": user, "tenant": tenant, "conflicts": conflicts, "suspicious": suspicious,
        "flash": flash,
    })


@router.post("/conflicts/resolve")
async def conflicts_resolve(request: Request, phone: str = Form(...),
                            keep: str = Form(""), action: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    from app import user_db
    if action not in ("merge", "both", "archive"):
        return RedirectResponse("/admin/conflicts", status_code=303)
    phone = "".join(c for c in phone if c.isdigit())
    try:
        res = await run_in_threadpool(user_db.resolve_phone_conflict, user["tenant_id"], phone, keep or None, action)
    except Exception as e:
        return _render(request, "admin_conflicts.html", {
            "user": user, "tenant": _tenant_of(user),
            "conflicts": await run_in_threadpool(user_db.list_phone_conflicts, user["tenant_id"]),
            "suspicious": await run_in_threadpool(user_db.get_suspicious_phones, user["tenant_id"]),
            "flash": f"❌ خطا در حل تعارض: {e}",
        })
    if res.get("error"):
        flash = f"❌ {res['error']}"
    elif action == "merge":
        flash = f"✅ ادغام شد — {res.get('merged_requests', 0)} درخواست منتقل و شمارنده {res.get('total_count', 0)} شد."
    elif action == "both":
        flash = f"✅ هر دو پروفایل واقعی ثبت شدند و از صف خارج شدند."
    else:
        flash = f"✅ {res.get('archived', 0)} پروفایل بایگانی شد."
    return RedirectResponse("/admin/conflicts?ok=1", status_code=303)

