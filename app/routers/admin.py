# -*- coding: utf-8 -*-
"""Admin panel — tenant panel (admin) + super-admin panel"""
import json
import hmac
import re
from datetime import datetime

from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.config import (
    BASE_DIR, DATA_DIR, PROFILES_JSON, USER_RECORDS_DB, APPOINTMENTS_DB,
)
from app.db import get_conn
from app import auth
from app import tenants as tenants_mod
from app.admin_tools import export_users_excel
from app.excel_to_json import convert_excel_to_json
from app.user_db import replace_consultants, get_consultants_stats
from app.internal_ai_engine import (
    record_feedback_direct, get_learning_stats, get_learning_weight,
    names_match, engine,
)
from app.crawler import crawl_available_slots
from app import session_store as ss

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

FEEDBACK_CONCEPTS = {
    "1": ("اضطراب", "اضطراب"), "2": ("افسردگی", "افسردگی"), "3": ("وسواس", "وسواس"),
    "4": ("زوج/ازدواج", "زوج_ازدواج"), "5": ("کودک", "کودک"), "6": ("نوجوان/جوان", "نوجوان_جوان"),
    "7": ("والد-فرزند", "والد_فرزند"), "8": ("ارتباط/تعارض", "ارتباط_تعارض"),
    "9": ("توسعه فردی", "توسعه_فردی"), "10": ("حقوقی", "حقوقی"),
    "11": ("روانپزشکی", "پزشکی_روانپزشکی"),
}


# ── helpers ──

def _render(request: Request, template: str, ctx: dict, status_code: int = 200):
    return TEMPLATES.TemplateResponse(request, template, ctx, status_code=status_code)


def _current_user(request: Request) -> dict | None:
    return auth.get_current_user(request)


def _tenant_of(user: dict) -> dict:
    t = tenants_mod.get_tenant(user["tenant_id"])
    if not t:
        raise HTTPException(status_code=404, detail="مشتری یافت نشد.")
    return t


# ── Login / logout ──

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _current_user(request):
        return RedirectResponse("/admin", status_code=303)
    return _render(request, "admin_login.html", {"error": None})


# Login rate limit: max 10 attempts per 5 minutes per IP (anti brute-force)
import time as _time
_login_hits: dict[str, list[float]] = {}
_LOGIN_MAX_KEYS = 5000


def _login_allowed(ip: str) -> bool:
    now = _time.time()
    if len(_login_hits) > _LOGIN_MAX_KEYS:
        cutoff = now - 300
        for k in [k for k, v in _login_hits.items() if not v or v[-1] < cutoff]:
            _login_hits.pop(k, None)
    hits = _login_hits.get(ip, [])
    hits = [t for t in hits if now - t < 300]
    _login_hits[ip] = hits
    if len(hits) >= 10:
        return False
    hits.append(now)
    return True


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    client_ip = request.client.host if request.client else "unknown"
    if not _login_allowed(client_ip):
        return _render(request, "admin_login.html",
                       {"error": "تلاش‌های ورود بیش از حد مجاز. ۵ دقیقه صبر کنید."}, 429)
    user = await run_in_threadpool(auth.authenticate, username.strip(), password)
    if not user:
        return _render(request, "admin_login.html", {"error": "نام کاربری یا رمز عبور نادرست است."}, 401)
    from app.config import COOKIE_SECURE
    resp = RedirectResponse("/admin", status_code=303)
    auth.set_session_cookie(resp, user, secure=COOKIE_SECURE)
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    auth.clear_session_cookie(resp)
    return resp


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        return await _super_dashboard(request, user)
    return await _tenant_dashboard(request, user, flash=None)


# ── Tenant panel (admin) ──

def _collect_tenant_stats(tenant_id: int) -> dict:
    conn = get_conn(USER_RECORDS_DB)
    try:
        total_users = conn.execute("SELECT COUNT(*) FROM users WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
        total_requests = conn.execute("SELECT COUNT(*) FROM user_requests WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
        active_chats = conn.execute(
            "SELECT COUNT(*) FROM widget_sessions WHERE tenant_id=? AND state != -2 AND expires_at >= datetime('now')",
            (tenant_id,),
        ).fetchone()[0]
        total_chats = conn.execute("SELECT COUNT(*) FROM widget_sessions WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
    finally:
        conn.close()

    stats = get_consultants_stats(tenant_id)

    # Appointment stats (same as the bot's /stats — Nikravan only), with site data broken out separately from active Excel counselors
    appointments = {}
    free_counselors = 0
    total_free_slots = 0
    site_total = 0
    top_free = []
    active_with_free = 0
    crawl_health = None
    if tenant_id == 1:
        try:
            aconn = get_conn(APPOINTMENTS_DB)
            appointments = dict(aconn.execute("SELECT status, COUNT(*) FROM appointments GROUP BY status").fetchall())
            free_counselors = aconn.execute("SELECT COUNT(DISTINCT counselor_name) FROM appointments WHERE status='free'").fetchone()[0]
            total_free_slots = aconn.execute("SELECT COUNT(*) FROM appointments WHERE status='free'").fetchone()[0]
            site_total = aconn.execute("SELECT COUNT(DISTINCT counselor_name) FROM appointments").fetchone()[0]
            top_free = aconn.execute(
                "SELECT counselor_name, COUNT(*) FROM appointments WHERE status='free' GROUP BY counselor_name ORDER BY 2 DESC LIMIT 5"
            ).fetchall()
            aconn.close()
            # How many of the active Excel counselors have free slots — fuzzy name matching (same as the bot)
            aconn2 = get_conn(APPOINTMENTS_DB)
            site_free_names = [r[0] for r in aconn2.execute(
                "SELECT DISTINCT counselor_name FROM appointments WHERE status='free'"
            ).fetchall()]
            aconn2.close()
            from app.internal_ai_engine import names_match
            uconn = get_conn(USER_RECORDS_DB)
            excel_names = [r[0] for r in uconn.execute(
                "SELECT name FROM consultants WHERE tenant_id=1"
            ).fetchall()]
            uconn.close()
            active_with_free = sum(1 for e in excel_names if any(names_match(e, s) for s in site_free_names))
        except Exception:
            pass
        try:
            from app.crawler import get_last_crawl_info
            from app.config import CRAWL_STALE_AFTER_SECONDS
            from datetime import datetime as _dt2, timedelta as _td2
            info = get_last_crawl_info()
            if info:
                try:
                    finished = _dt2.strptime(info["finished_at"], "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    finished = None
                age_min = round((_dt2.now() - finished).total_seconds() / 60) if finished else None
                crawl_health = {
                    **info,
                    "age_minutes": age_min,
                    "stale": age_min is None or age_min * 60 > CRAWL_STALE_AFTER_SECONDS,
                }
        except Exception:
            pass

    return {
        "total_users": total_users,
        "total_requests": total_requests,
        "active_chats": active_chats,
        "total_chats": total_chats,
        "consultant_count": stats["total"],
        "consultants_full": stats,
        "appointments": appointments,
        "free_counselors": free_counselors,
        "total_free_slots": total_free_slots,
        "site_total": site_total,
        "top_free": top_free,
        "active_with_free": active_with_free,
        "crawl_health": crawl_health,
    }


async def _tenant_dashboard(request: Request, user: dict, flash: str | None):
    tenant = _tenant_of(user)
    tenant_id = tenant["id"]
    stats = await run_in_threadpool(_collect_tenant_stats, tenant_id)
    learning = await run_in_threadpool(get_learning_stats, tenant_id)
    counselor_names = await run_in_threadpool(lambda: sorted({p["clean_name"] for p in engine.get_profiles(tenant_id)}))
    concept_labels = {k: v[0] for k, v in FEEDBACK_CONCEPTS.items()}
    announcement = await run_in_threadpool(_get_announcement, tenant_id)
    excel_info = await run_in_threadpool(_current_excel_info, tenant_id)
    return _render(request, "admin_dashboard.html", {
        "user": user, "tenant": tenant, "stats": stats, "learning": learning,
        "counselor_names": counselor_names, "concepts": concept_labels,
        "flash": flash, "announcement": announcement,
        "flow": tenant["flow_config"],
        "step_types": {"text": "متن آزاد", "phone": "شماره تماس", "number": "عدد", "choice": "گزینه‌ای"},
        "CRAWL_LABELS": {
            "free": "🟢 نوبت آزاد", "waiting": "🟡 لیست انتظار",
            "no_available": "🔴 بدون نوبت فعلی", "no_free_slots": "🔴 بدون نوبت آزاد",
            "phone_only": "📞 صرفاً تماس تلفنی", "no_table": "⚠️ جدول نوبت یافت نشد",
        },
        "crawl_running": _crawl_state["running"],
        "excel_info": excel_info,
    })


def _current_excel_info(tenant_id: int) -> dict:
    """Active Excel file info (latest upload) + 5 sample rows"""
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT consultant_count, uploaded_at FROM upload_history WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    samples = conn.execute(
        "SELECT name, ability, location, age_range, education_experience FROM consultants WHERE tenant_id=? ORDER BY id LIMIT 5",
        (tenant_id,),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM consultants WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
    conn.close()
    import glob
    files = sorted(glob.glob(str(DATA_DIR / f"consultants_{tenant_id}_*.xlsx")))
    filename = files[-1].split("\\")[-1].split("/")[-1] if files else None
    return {
        "uploaded_at": row[1] if row else None,
        "uploaded_count": row[0] if row else None,
        "total": total,
        "filename": filename,
        "samples": samples,
    }


def _get_announcement(tenant_id: int) -> str | None:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT text FROM announcements WHERE tenant_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ── Conversations (inbox) ──

@router.get("/inbox", response_class=HTMLResponse)
async def inbox(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    tenant_id = user["tenant_id"] if user["role"] == "admin" else None
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    chats = await run_in_threadpool(_list_chats, tenant_id)
    return _render(request, "admin_inbox.html", {"user": user, "chats": chats})


def _list_chats(tenant_id: int) -> list:
    conn = get_conn(USER_RECORDS_DB)
    rows = conn.execute("""
        SELECT s.token, s.state, s.data, s.created_at, s.updated_at,
               (SELECT text FROM widget_messages m WHERE m.token=s.token ORDER BY m.id DESC LIMIT 1) AS last_msg,
               (SELECT COUNT(*) FROM widget_messages m WHERE m.token=s.token) AS msg_count
        FROM widget_sessions s WHERE s.tenant_id=?
        ORDER BY s.updated_at DESC LIMIT 200
    """, (tenant_id,)).fetchall()
    conn.close()
    chats = []
    for (token, state, data_json, created, updated, last_msg, msg_count) in rows:
        data = json.loads(data_json or "{}")
        state_label = {
            -2: "✅ تکمیل‌شده", -3: "📋 GHQ",
            -1: "📋 GHQ",
        }.get(state, f"⏳ مرحله {state + 1}")
        chats.append({
            "token": token, "name": data.get("full_name") or "بازدیدکننده",
            "phone": data.get("phone", ""), "state_label": state_label,
            "created": created, "updated": updated, "last_msg": (last_msg or "")[:60],
            "msg_count": msg_count,
        })
    return chats


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


# ── Requests ──

@router.get("/leads", response_class=HTMLResponse)
async def leads(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    rows = await run_in_threadpool(_list_leads, user["tenant_id"])
    return _render(request, "admin_leads.html", {"user": user, "rows": rows})


def _list_leads(tenant_id: int) -> list:
    conn = get_conn(USER_RECORDS_DB)
    rows = conn.execute("""
        SELECT id, full_name, phone, gender, age, topic, ghq_total, request_number, created_at, recommendations
        FROM user_requests WHERE tenant_id=? ORDER BY created_at DESC LIMIT 500
    """, (tenant_id,)).fetchall()
    conn.close()
    out = []
    for (rid, name, phone, gender, age, topic, ghq, rn, created, recs_json) in rows:
        rec_names = ""
        try:
            recs = json.loads(recs_json) if recs_json else []
            rec_names = "، ".join(r.get("name", "") for r in recs if isinstance(r, dict))
        except Exception:
            pass
        out.append({"id": rid, "name": name, "phone": phone, "gender": gender,
                    "age": age, "topic": topic, "ghq": ghq, "rn": rn,
                    "created": created, "recs": rec_names})
    return out


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


# ── Phone-number conflicts (Nikravan and every tenant — per-tenant) ──

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


# ── Announcement ──

@router.post("/announcement")
async def save_announcement(request: Request, text: str = Form(""), active: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]

    def _save():
        conn = get_conn(USER_RECORDS_DB)
        conn.execute("UPDATE announcements SET active=0 WHERE tenant_id=?", (tenant_id,))
        if active == "1" and text.strip():
            conn.execute(
                "INSERT INTO announcements (tenant_id, text, active) VALUES (?,?,1)",
                (tenant_id, text.strip()),
            )
        conn.commit()
        conn.close()

    await run_in_threadpool(_save)
    flash = "✅ اطلاعیه فعال شد و در باز شدن بعدی ویجت نمایش داده می‌شود." if active == "1" else "اطلاعیه غیرفعال شد."
    return await _tenant_dashboard(request, user, flash=flash)


# ── Flow configuration ──

@router.post("/flow")
async def save_flow(request: Request,
                    welcome: str = Form(""),
                    ghq: str = Form(""),
                    ending: str = Form("lead_capture"),
                    thanks_text: str = Form(""),
                    steps_json: str = Form("[]")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]

    try:
        steps = json.loads(steps_json)
        if not isinstance(steps, list) or not steps:
            raise ValueError
        clean_steps = []
        for s in steps:
            key = str(s.get("key", "")).strip() or f"q_{len(clean_steps) + 1}"
            stype = s.get("type", "text")
            if stype not in ("text", "phone", "number", "choice"):
                stype = "text"
            step = {"key": key, "type": stype}
            label = s.get("label") or ""
            if str(label).strip():
                step["label"] = str(label).strip()
            if stype == "choice":
                raw_opts = s.get("options") or ""
                if isinstance(raw_opts, list):
                    opts = [str(o).strip() for o in raw_opts if str(o).strip()]
                else:
                    opts = [o.strip() for o in str(raw_opts).split("|") if o.strip()]
                if not opts:
                    opts = ["بله", "خیر"]
                step["options"] = opts
            # keep the step condition (e.g. prev_detail only when prev_therapy=yes)
            cond = str(s.get("conditional") or "").strip()
            if "=" in cond:
                cond_key, _, cond_val = cond.partition("=")
                cond_key = cond_key.strip()
                cond_val = cond_val.strip()
                # the condition may refer to the key or source of a previous step (e.g. has_prev_therapy instead of prev_therapy)
                if cond_key and any(
                    c.get("key") == cond_key or c.get("source") == cond_key for c in clean_steps
                ):
                    step["conditional"] = cond
                else:
                    # legacy condition using the standard field name — map it to the previous step's key
                    _SOURCE_TO_KEY = {"has_prev_therapy": "prev_therapy", "full_name": "full_name",
                                      "phone": "phone", "gender": "gender", "age": "age",
                                      "topic": "topic", "expectation": "expectation",
                                      "preferred_gender": "preferred_gender", "branch": "branch",
                                      "prev_detail": "prev_detail"}
                    target_key = _SOURCE_TO_KEY.get(cond_key, cond_key)
                    for prev in clean_steps:
                        if prev["key"] == target_key or (prev.get("source") or prev["key"]) == cond_key:
                            step["conditional"] = f"{prev['key']}={cond_val}"
                            break
            clean_steps.append(step)
        if ending not in ("recommend", "lead_capture"):
            ending = "lead_capture"
        flow = {
            "welcome": welcome.strip() or "سلام! خوش آمدید.",
            "steps": clean_steps,
            "ghq": ghq == "1",
            "ending": ending,
            "thanks_text": thanks_text.strip() or "متشکریم! اطلاعات شما ثبت شد.",
        }
        await run_in_threadpool(tenants_mod.save_flow_config, tenant_id, flow)
        # the engine's profile cache for this tenant may have changed
        return await _tenant_dashboard(request, user, flash="✅ فلوی گفتگو ذخیره شد.")
    except (ValueError, json.JSONDecodeError):
        return await _tenant_dashboard(request, user, flash="❌ فرمت مراحل نامعتبر است.")


# ── Counselors ──

def _process_upload_sync(xlsx_bytes: bytes, tenant_id: int) -> dict:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = DATA_DIR / f"consultants_{tenant_id}_{stamp}.xlsx"
    saved.write_bytes(xlsx_bytes)
    # convert to a temporary JSON, then insert into the DB (without overwriting the Nikravan JSON)
    tmp_json = DATA_DIR / f"consultants_{tenant_id}_tmp.json"
    count_json = convert_excel_to_json(str(saved), str(tmp_json))
    if count_json == 0:
        tmp_json.unlink(missing_ok=True)
        saved.unlink(missing_ok=True)
        raise ValueError("هیچ مشاوری از فایل استخراج نشد — قالب ستون‌ها را بررسی کنید.")
    profiles = json.loads(tmp_json.read_text(encoding="utf-8"))
    compare = replace_consultants(profiles, uploaded_by=0, tenant_id=tenant_id)
    tmp_json.unlink(missing_ok=True)
    return {
        "json_count": count_json,
        "db_count": compare["inserted"],
        "compare": compare,
        "saved_name": saved.name,
        "file_size": len(xlsx_bytes),
    }


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        return await _consultants_page(request, user, flash="❌ حجم فایل بیش از ۱۰ مگابایت است.")
    if not content.startswith(b"PK"):
        return await _consultants_page(request, user, flash="❌ فایل معتبر اکسل (xlsx) نیست.")
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        return await _consultants_page(request, user, flash="❌ لطفاً فقط فایل اکسل (.xlsx) ارسال فرمایید.")

    try:
        result = await run_in_threadpool(_process_upload_sync, content, tenant_id)
        engine.set_tenant(tenant_id, force=True)  # reload the profile cache from the DB
        stats = await run_in_threadpool(get_consultants_stats, tenant_id)
        comp = result["compare"]
        added, removed = comp.get("added", []), comp.get("removed", [])
        flash = (
            f"✅ آپلود موفق — {result['saved_name']} ({result['file_size'] // 1024} KB)\n"
            f"• ردیف‌های اکسل: {result['json_count']} | درج در DB: {result['db_count']}\n"
            f"• تعداد قبلی: {comp.get('old_count')} ← جدید: {comp.get('new_count')}\n"
            f"• ➕ جدید ({len(added)}): {'، '.join(added[:6]) if added else 'ندارد'}\n"
            f"• ➖ حذف ({len(removed)}): {'، '.join(removed[:6]) if removed else 'ندارد'}\n\n"
            f"📋 صحت داده در دیتابیس (فیلدهای پرشده):\n"
            f"• کل رکوردها: {stats['total']} | ضریب توانمندی: {stats['with_ability']} | محل کار: {stats['with_location']}\n"
            f"• تحصیلات/سوابق: {stats['with_education']} | حوزه عمومی: {stats['with_general_area']} | محدوده سنی: {stats['with_age_range']}\n"
            f"• پروانه: {stats['with_license']} | ملاحظات: {stats['with_notes']}\n"
            f"• دارای تخصص جزئی: {stats['with_specializations']} مشاور (مجموع {stats['spec_total']} تخصص)\n\n"
            f"• موتور هوشمند بدون ری‌استارت به‌روزرسانی شد"
        )
        return await _consultants_page(request, user, flash=flash)
    except Exception as e:
        return await _consultants_page(request, user, flash=f"❌ خطا در پردازش اکسل: {e}")


async def _consultants_page(request: Request, user: dict, flash: str | None):
    tenant = _tenant_of(user)
    excel_info = await run_in_threadpool(_current_excel_info, tenant["id"])
    stats = await run_in_threadpool(get_consultants_stats, tenant["id"])
    rows = await run_in_threadpool(_list_consultants, tenant["id"])
    return _render(request, "admin_consultants.html", {
        "user": user, "tenant": tenant, "excel_info": excel_info,
        "stats": stats, "rows": rows, "flash": flash,
    })


@router.get("/consultants")
async def consultants_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    return await _consultants_page(request, user, flash=None)


def _list_consultants(tenant_id: int) -> list:
    conn = get_conn(USER_RECORDS_DB)
    rows = conn.execute(
        "SELECT name, ability, location, age_range, education_experience, general_area, license FROM consultants WHERE tenant_id=? ORDER BY name",
        (tenant_id,),
    ).fetchall()
    conn.close()
    return rows


@router.get("/consultants/download")
async def download_current_excel(request: Request):
    """Download the Excel file the engine data was most recently built from"""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]

    def _build():
        from openpyxl import Workbook
        conn = get_conn(USER_RECORDS_DB)
        rows = conn.execute(
            """SELECT name, ability, location, education_experience, general_area,
                      general_area_2, specializations, detailed_topics, age_range, license, notes
               FROM consultants WHERE tenant_id=? ORDER BY id""", (tenant_id,),
        ).fetchall()
        conn.close()
        wb = Workbook()
        ws = wb.active
        ws.title = "مشاوران"
        ws.append(["نام", "ضریب توانمندی", "محل کار", "تحصیلات و سوابق", "حوزه عمومی",
                   "حوزه عمومی ۲", "تخصص‌ها (JSON)", "موضوعات جزئی", "محدوده سنی", "پروانه", "ملاحظات"])
        def _safe(v):
            # prevent formula injection: text cells starting with = + - @
            if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
                return "'" + v
            return v

        for r in rows:
            ws.append([_safe(v) for v in r])
        import io
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    data = await run_in_threadpool(_build)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=current_consultants_{tenant_id}_{stamp}.xlsx"},
    )


# ── Feedback and learning ──

@router.post("/feedback")
async def feedback(request: Request, counselor_name: str = Form(...), concept_num: str = Form(...), verdict: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]

    concept = FEEDBACK_CONCEPTS.get(concept_num)
    if not concept:
        return await _tenant_dashboard(request, user, flash="❌ شماره مفهوم نامعتبر است.")
    concept_label, concept_key = concept

    profiles = engine.get_profiles(tenant_id)
    matched = next((p["clean_name"] for p in profiles if names_match(p["clean_name"], counselor_name)), None)
    if not matched:
        return await _tenant_dashboard(request, user, flash=f"❌ مشاور «{counselor_name}» در لیست فعال یافت نشد.")

    success = verdict == "بله"
    ok = record_feedback_direct(matched, concept_key, success, tenant_id)
    if not ok:
        return await _tenant_dashboard(request, user, flash="❌ خطا در ثبت بازخورد.")

    w = get_learning_weight(matched, concept_key, tenant_id)
    arrow = "⬆️" if success else "⬇️"
    flash = f"🧠 بازخورد ثبت شد: {matched} — {concept_label} {arrow} وزن فعلی: ×{w:.2f}"
    return await _tenant_dashboard(request, user, flash=flash)


# ── Manual crawl (Nikravan only) — runs in the background because it takes ~5 minutes ──

import asyncio as _asyncio
from datetime import datetime as _dt

_crawl_state = {
    "running": False, "last_finished": None, "started_at": None,
    "total": 0, "done": 0, "current": "", "free_total": 0, "error": None,
}


def _crawl_background():
    _crawl_state.update(running=True, total=0, done=0, current="", free_total=0,
                        error=None, started_at=_dt.now().strftime("%H:%M:%S"))
    try:
        from app import crawler as _crawler

        def _progress(done: int, total: int, current: str, free_total: int):
            _crawl_state.update(done=done, total=total, current=current, free_total=free_total)

        crawl_available_slots(progress_cb=_progress)
        _crawl_state["last_finished"] = _dt.now().strftime("%H:%M:%S")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("manual crawl error: %s", e)
        _crawl_state["error"] = str(e)[:200]
        try:
            from app.crawler import record_crawl_run
            record_crawl_run(0, 0, 0, ok=False, error=str(e))
        except Exception:
            pass
    finally:
        _crawl_state["running"] = False


@router.get("/crawl/status")
async def crawl_status(request: Request):
    """Live status of the manual crawl for dashboard page polling"""
    from fastapi.responses import JSONResponse
    user = _current_user(request)
    if not user:
        return JSONResponse({"running": False, "login": True}, status_code=401)
    if user["role"] == "super_admin":
        return JSONResponse({"running": False}, status_code=403)
    tenant = _tenant_of(user)
    if tenant["slug"] != "nikravan":
        return JSONResponse({"running": False}, status_code=403)
    return JSONResponse({k: _crawl_state[k] for k in
                         ("running", "last_finished", "started_at", "total", "done",
                          "current", "free_total", "error")})


@router.post("/crawl")
async def manual_crawl(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant = _tenant_of(user)
    if tenant["slug"] != "nikravan":
        return await _tenant_dashboard(request, user, flash="کراول نوبت فقط برای نیک‌روان فعال است.")
    if _crawl_state["running"]:
        return await _tenant_dashboard(request, user,
            flash="⏳ کراول قبلی هنوز در حال اجراست (حدود ۵ دقیقه طول می‌کشد). چند دقیقه دیگر صفحه را رفرش کنید.")
    _crawl_state["running"] = True
    loop = _asyncio.get_running_loop()
    loop.run_in_executor(None, _crawl_background)
    return await _tenant_dashboard(request, user,
        flash="⏳ کراول نوبت‌ها در پس‌زمینه آغاز شد (۸۶ صفحه سایت — حدود ۵ دقیقه). چند دقیقه دیگر رفرش کنید تا آمار جدید را ببینید.")


# ── Change own password ──

@router.get("/password", response_class=HTMLResponse)
async def password_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="برای تغییر رمز با سوپر ادمین تماس بگیرید.")
    return _render(request, "admin_password.html", {"user": user, "error": None, "ok": None})


@router.post("/password")
async def change_password(request: Request,
                          current_password: str = Form(""),
                          new_password: str = Form(""),
                          new_password2: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="برای تغییر رمز با سوپر ادمین تماس بگیرید.")
    if not auth.authenticate(user["username"], current_password):
        return _render(request, "admin_password.html", {"user": user, "error": "رمز فعلی نادرست است.", "ok": None}, 401)
    if len(new_password) < 8:
        return _render(request, "admin_password.html", {"user": user, "error": "رمز جدید باید حداقل ۸ کاراکتر باشد.", "ok": None}, 400)
    if len(new_password) > 128:
        return _render(request, "admin_password.html", {"user": user, "error": "رمز جدید حداکثر ۱۲۸ کاراکتر باشد.", "ok": None}, 400)
    if new_password != new_password2:
        return _render(request, "admin_password.html", {"user": user, "error": "تکرار رمز جدید مطابقت ندارد.", "ok": None}, 400)

    def _save():
        conn = get_conn(USER_RECORDS_DB)
        conn.execute(
            "UPDATE panel_users SET password_hash=? WHERE id=?",
            (auth.hash_password(new_password), user["id"]),
        )
        conn.commit()
        conn.close()

    await run_in_threadpool(_save)
    return _render(request, "admin_password.html", {"user": user, "error": None, "ok": "رمز عبور با موفقیت تغییر کرد."})


# ══════════ Super-admin panel ══════════

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
    return _render(request, "super_tenants.html", {
        "user": user, "tenants": tenants_list, "flash": flash,
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
        return await _super_dashboard(request, user, flash=f"✅ ادمین «{admin_username}» به مشتری اضافه شد.")
    except Exception:
        return await _super_dashboard(request, user, flash="❌ این نام کاربری قبلاً استفاده شده است.")
