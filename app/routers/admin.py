# -*- coding: utf-8 -*-
"""پنل مدیریت — داشبورد، ورود، فلو، مشاوران، کراول (inbox/leads/super جدا شدند)"""
import json
import re
from datetime import datetime

from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from app.config import (
    BASE_DIR, DATA_DIR, USER_RECORDS_DB, APPOINTMENTS_DB,
)
from app.db import get_conn
from app import auth
from app import tenants as tenants_mod
from app.excel_to_json import convert_excel_to_json
from app.user_db import replace_consultants, get_consultants_stats
from app.internal_ai_engine import (
    record_feedback_direct, get_learning_stats, get_learning_weight,
    names_match, engine,
)
from app.crawler import crawl_available_slots
from app.routers.panel_common import _render, _current_user, _tenant_of, TEMPLATES
from app.routers.super_routes import _super_dashboard

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

FEEDBACK_CONCEPTS = {
    "1": ("اضطراب", "اضطراب"), "2": ("افسردگی", "افسردگی"), "3": ("وسواس", "وسواس"),
    "4": ("زوج/ازدواج", "زوج_ازدواج"), "5": ("کودک", "کودک"), "6": ("نوجوان/جوان", "نوجوان_جوان"),
    "7": ("والد-فرزند", "والد_فرزند"), "8": ("ارتباط/تعارض", "ارتباط_تعارض"),
    "9": ("توسعه فردی", "توسعه_فردی"), "10": ("حقوقی", "حقوقی"),
    "11": ("روانپزشکی", "پزشکی_روانپزشکی"),
}


# ── helpers: از panel_common ──


# ── ورود / خروج ──

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _current_user(request):
        return RedirectResponse("/admin", status_code=303)
    return _render(request, "admin_login.html", {"error": None})


# Rate limit ورود: حداکثر ۱۰ تلاش در ۵ دقیقه به‌ازای IP (ضد brute-force، دیتابیس‌محور)
from app.ratelimit import client_ip as _client_ip, rate_ok as _rate_ok

_LOGIN_LIMIT = 10
_LOGIN_WINDOW = 300


def _login_allowed(ip: str) -> bool:
    return _rate_ok(f"login:{ip}", _LOGIN_LIMIT, _LOGIN_WINDOW)


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    if not _login_allowed(_client_ip(request)):
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


# ── پنل مشتری (ادمین) ──

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

    # آمار نوبت‌ها (عین /stats ربات — فقط tenant نمونه) با تفکیک سایت از مشاوران فعال اکسل
    appointments = {}
    free_counselors = 0
    total_free_slots = 0
    site_total = 0
    top_free = []
    active_with_free = 0
    crawl_health = None
    if tenant_id == 1:
        try:
            # نوبت‌ها حالا به مرکزِ صاحبشان قفل‌اند؛ شمارشِ داشبورد هم باید
            # همان فیلتر را داشته باشد، وگرنه به‌محض کراولِ مرکزِ دوم،
            # آمارِ tenant نمونه جمعِ همهٔ مراکز می‌شد.
            aconn = get_conn(APPOINTMENTS_DB)
            appointments = dict(aconn.execute(
                "SELECT status, COUNT(*) FROM appointments WHERE tenant_id=? GROUP BY status",
                (tenant_id,)).fetchall())
            free_counselors = aconn.execute(
                "SELECT COUNT(DISTINCT counselor_name) FROM appointments WHERE tenant_id=? AND status='free'",
                (tenant_id,)).fetchone()[0]
            total_free_slots = aconn.execute(
                "SELECT COUNT(*) FROM appointments WHERE tenant_id=? AND status='free'",
                (tenant_id,)).fetchone()[0]
            site_total = aconn.execute(
                "SELECT COUNT(DISTINCT counselor_name) FROM appointments WHERE tenant_id=?",
                (tenant_id,)).fetchone()[0]
            top_free = aconn.execute(
                "SELECT counselor_name, COUNT(*) FROM appointments WHERE tenant_id=? AND status='free' "
                "GROUP BY counselor_name ORDER BY 2 DESC LIMIT 5", (tenant_id,)).fetchall()
            aconn.close()
            # چند نفر از مشاوران فعال اکسل نوبت آزاد دارند — تطابق فازی نام (عین ربات)
            aconn2 = get_conn(APPOINTMENTS_DB)
            site_free_names = [r[0] for r in aconn2.execute(
                "SELECT DISTINCT counselor_name FROM appointments WHERE tenant_id=? AND status='free'",
                (tenant_id,)).fetchall()]
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
    from app import plan_service
    can_learn = await run_in_threadpool(
        plan_service.tenant_has_mode, tenant_id, plan_service.MODE_ADVANCED,
    )
    learning = await run_in_threadpool(get_learning_stats, tenant_id) if can_learn else []
    counselor_names = await run_in_threadpool(lambda: sorted({p["clean_name"] for p in engine.get_profiles(tenant_id)}))
    concept_labels = {k: v[0] for k, v in FEEDBACK_CONCEPTS.items()}
    announcement = await run_in_threadpool(_get_announcement, tenant_id)
    excel_info = await run_in_threadpool(_current_excel_info, tenant_id)
    plan_state = await run_in_threadpool(plan_service.get_tenant_plan_state, tenant_id)
    plan_lock_message = await run_in_threadpool(plan_service.lock_message_for, tenant_id)
    from app import crawl_state as _csm
    crawl_running = (await run_in_threadpool(_csm.get_status))["running"]
    return _render(request, "admin_dashboard.html", {
        "user": user, "tenant": tenant, "stats": stats, "learning": learning,
        "can_learn": can_learn,
        "counselor_names": counselor_names, "concepts": concept_labels,
        "flash": flash, "announcement": announcement,
        "flow": tenant["flow_config"],
        "step_types": {"text": "متن آزاد", "phone": "شماره تماس", "number": "عدد", "choice": "گزینه‌ای"},
        "CRAWL_LABELS": {
            "free": "🟢 نوبت آزاد", "waiting": "🟡 لیست انتظار",
            "no_available": "🔴 بدون نوبت فعلی", "no_free_slots": "🔴 بدون نوبت آزاد",
            "phone_only": "📞 صرفاً تماس تلفنی", "no_table": "⚠️ جدول نوبت یافت نشد",
        },
        "crawl_running": crawl_running,
        "excel_info": excel_info,
        "plan_state": plan_state,
        "plan_mode_labels": plan_service.MODE_LABELS,
        "quota_labels": {
            "msg": plan_service.quota_label(
                plan_state["messages_used"], plan_state["effective_message_quota"]
            ) if plan_state else "—",
            "intro": plan_service.quota_label(
                plan_state["introductions_used"], plan_state["effective_introduction_quota"]
            ) if plan_state else "—",
        },
        "lock_message": plan_lock_message,
    })


# ── بستهٔ اضافه (مشتری می‌خرد، سوپرادمین تأیید می‌کند) ──

async def _topups_page(request: Request, user: dict, flash: str | None = None):
    from app import plan_service as _ps
    tenant = _tenant_of(user)
    tid = tenant["id"]
    packages = await run_in_threadpool(_ps.list_packages)
    plan_state = await run_in_threadpool(_ps.get_tenant_plan_state, tid)
    mine = await run_in_threadpool(_ps.list_topup_requests, tid, "")
    return _render(request, "admin_topups.html", {
        "user": user, "tenant": tenant, "packages": packages,
        "plan_state": plan_state, "requests": mine, "flash": flash,
        "price_label": _ps.rial_label,
        "quota_labels": {
            "msg": _ps.quota_label(
                plan_state["messages_used"], plan_state["effective_message_quota"]
            ) if plan_state else "—",
            "intro": _ps.quota_label(
                plan_state["introductions_used"], plan_state["effective_introduction_quota"]
            ) if plan_state else "—",
        },
    })


@router.get("/topups", response_class=HTMLResponse)
async def topups_page_route(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    return await _topups_page(request, user)


@router.post("/topups/buy")
async def topup_buy(request: Request, package_id: int = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    from app import plan_service as _ps
    try:
        await run_in_threadpool(
            _ps.request_topup, user["tenant_id"], package_id, user["username"])
    except ValueError as e:
        return await _topups_page(request, user, flash=f"❌ {e}")
    return await _topups_page(
        request, user,
        flash="🛒 درخواست خرید ثبت شد. پس از تأیید و پرداخت، اعتبار شما اضافه می‌شود.")


def _current_excel_info(tenant_id: int) -> dict:
    """اطلاعات فایل اکسل فعال (آخرین آپلود) + ۵ ردیف نمونه"""
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



# ── اطلاعیه ──

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


# ── تنظیم فلو ──

@router.post("/flow")
async def save_flow(request: Request,
                    welcome: str = Form(""),
                    ghq: str = Form(""),
                    ending: str = Form("lead_capture"),
                    thanks_text: str = Form(""),
                    for_other: str = Form("1"),
                    steps_json: str = Form("[]")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]
    want_other = for_other == "1"

    try:
        steps = json.loads(steps_json)
        if not isinstance(steps, list) or not steps:
            raise ValueError
        # استپ رابطه با سوییچ جدا مدیریت می‌شود (Q23) — از لیست فرم حذف و دوباره درج/نادیده گرفته می‌شود.
        # فقط dict واقعی: ورودی غیرdict (رشته/عدد) نباید به AttributeError و ۵۰۰ برسد
        steps = [
            s for s in steps
            if isinstance(s, dict) and str(s.get("key", "")).strip() != "relationship"
        ]
        clean_steps = []
        for s in steps:
            key = str(s.get("key", "")).strip() or f"q_{len(clean_steps) + 1}"
            stype = s.get("type", "text")
            if stype not in ("text", "phone", "number", "choice"):
                stype = "text"
            step = {"key": key, "type": stype}
            # source استپ حفظ شود (ادیتور فلو در POST نمی‌فرستد — پس default):
            # بدون sourceِ درست، «خیر»ِ سابقه درمان truthy می‌ماند و در گزارش‌ها
            # «بله» ثبت می‌شود؛ prev_therapy کلیدِ فیلد استانداردش has_prev_therapy است
            src = str(s.get("source") or "").strip()
            if src:
                step["source"] = src
            elif key == "prev_therapy":
                step["source"] = "has_prev_therapy"
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
            # حفظ شرط مرحله (مثل prev_detail فقط اگر prev_therapy=بله)
            cond = str(s.get("conditional") or "").strip()
            if "=" in cond:
                cond_key, _, cond_val = cond.partition("=")
                cond_key = cond_key.strip()
                cond_val = cond_val.strip()
                if cond_key == "relationship":
                    # استپ رابطه از این لیست حذف و بعداً prepend می‌شود — پس
                    # شرطِ relationship در clean_steps دیده نمی‌شود. بدون
                    # «برای دیگری» شرط هرگز برقرار نمی‌شود → کل مرحله حذف
                    # (مثل spouse_relation) تا بی‌شرط به همه تحمیل نشود.
                    if want_other:
                        step["conditional"] = cond
                    else:
                        continue
                elif cond_key and any(
                    c.get("key") == cond_key or c.get("source") == cond_key for c in clean_steps
                ):
                    step["conditional"] = cond
                else:
                    # شرط قدیمی با نام فیلد استاندارد — نگاشت به کلید مرحله قبل
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
                    if "conditional" not in step:
                        # شرط به استپی اشاره دارد که دیگر در فلو نیست —
                        # ذخیره بدون شرط یعنی سؤالِ بی‌شرط به همه تحمیل می‌شود
                        # (مثل prev_detail بدون سابقه درمان) → کل مرحله حذف
                        continue
            # محافظ در برابر شرطِ گم‌شدهٔ قدیمی: بدون استپ رابطه، سؤال همسر نباید بی‌شرط بماند
            if step["key"] == "spouse_relation" and not want_other:
                continue
            clean_steps.append(step)
        if ending not in ("recommend", "lead_capture"):
            ending = "lead_capture"
        from app.session_store import RELATIONSHIP_STEP
        if want_other:
            clean_steps = [dict(RELATIONSHIP_STEP)] + clean_steps
        # فیلتر بالا می‌تواند همه‌چیز را خالی کند (فقط relationship/غیرdict) —
        # ذخیرهٔ فلوی خالی گفتگو را می‌شکست، پس همان خطای فرمت را بده
        if not clean_steps:
            raise ValueError
        flow = {
            "welcome": welcome.strip() or "سلام! خوش آمدید.",
            "steps": clean_steps,
            "ghq": ghq == "1",
            "ending": ending,
            "for_other": want_other,
            "thanks_text": thanks_text.strip() or "متشکریم! اطلاعات شما ثبت شد.",
        }
        await run_in_threadpool(tenants_mod.save_flow_config, tenant_id, flow)
        # کش پروفایل موتور برای این tenant ممکن است تغییر کند
        return await _tenant_dashboard(request, user, flash="✅ فلوی گفتگو ذخیره شد.")
    except (ValueError, json.JSONDecodeError):
        return await _tenant_dashboard(request, user, flash="❌ فرمت مراحل نامعتبر است.")


# ── مشاوران ──

def _process_upload_sync(xlsx_bytes: bytes, tenant_id: int) -> dict:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = DATA_DIR / f"consultants_{tenant_id}_{stamp}.xlsx"
    saved.write_bytes(xlsx_bytes)
    # نسخه canonical «آخرین فایل خام» per-tenant برای دانلود بدون پردازش (Q37)
    uploads_dir = DATA_DIR / "uploads" / str(tenant_id)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    latest_raw = uploads_dir / "consultants_latest.xlsx"
    latest_raw.write_bytes(xlsx_bytes)
    # تبدیل به JSON موقت سپس درج در DB (بدون بازنویسی JSON مرکز)
    tmp_json = DATA_DIR / f"consultants_{tenant_id}_tmp.json"
    # نام شعبه‌های تعریف‌شدهٔ ادمین تا محل کار مشاور با همان نام‌ها چیده شود
    branches = None
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT flow_config FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    conn.close()
    if row:
        step = _branch_step_of(tenants_mod._parse_flow_config(row[0]))
        if step:
            branches = _editable_branches(step.get("options"))
    count_json = convert_excel_to_json(str(saved), str(tmp_json), branches=branches)
    if count_json == 0:
        tmp_json.unlink(missing_ok=True)
        saved.unlink(missing_ok=True)
        latest_raw.unlink(missing_ok=True)
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
    if len(content) > 5 * 1024 * 1024:  # سقف ۵MB (تصمیم Q37)
        return await _consultants_page(request, user, flash="❌ حجم فایل بیش از ۵ مگابایت است.")
    if not content.startswith(b"PK"):
        return await _consultants_page(request, user, flash="❌ فایل معتبر اکسل (xlsx) نیست.")
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        return await _consultants_page(request, user, flash="❌ لطفاً فقط فایل اکسل (.xlsx) ارسال فرمایید.")

    try:
        result = await run_in_threadpool(_process_upload_sync, content, tenant_id)
        engine.set_tenant(tenant_id, force=True)  # ری‌لود کش پروفایل‌ها از DB
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
    """دانلود فایل اکسل‌ی که همین حالا دیتای موتور از آن ساخته شده"""
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
            # جلوگیری از formula injection: سلول متنی که با = + - @ شروع شود
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


@router.get("/consultants/download-raw")
async def download_raw_excel(request: Request):
    """دانلود آخرین فایل اکسل بارگذاری‌شده — بدون هیچ پردازشی (Q15/Q37)"""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]
    from fastapi.responses import FileResponse
    # مسیر canonical: data/uploads/{tenant}/consultants_latest.xlsx (Q37)
    canonical = DATA_DIR / "uploads" / str(tenant_id) / "consultants_latest.xlsx"
    if canonical.exists():
        from app import audit_service as _audit
        _audit.log(user["username"], "excel.download_raw", {"tenant_id": tenant_id})
        return FileResponse(
            str(canonical),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"consultants_latest_{tenant_id}.xlsx",
        )
    # fallback: قدیمی‌ترین آپلودها (قبل از migration مسیر)
    import glob as _glob
    files = sorted(_glob.glob(str(DATA_DIR / f"consultants_{tenant_id}_*.xlsx")))
    files = [f for f in files if "_tmp" not in f]
    if not files:
        raise HTTPException(status_code=404, detail="هنوز فایلی بارگذاری نشده است.")
    latest = files[-1]
    filename = latest.split("\\")[-1].split("/")[-1]
    from app import audit_service as _audit
    _audit.log(user["username"], "excel.download_raw", {"tenant_id": tenant_id})
    return FileResponse(
        latest,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


# ── بازخورد و یادگیری ──

@router.post("/feedback")
async def feedback(request: Request, counselor_name: str = Form(...), concept_num: str = Form(...), verdict: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    tenant_id = user["tenant_id"]

    from app import plan_service
    if not await run_in_threadpool(
        plan_service.tenant_has_mode, tenant_id, plan_service.MODE_ADVANCED
    ):
        return await _tenant_dashboard(
            request, user,
            flash="❌ ثبت بازخورد و یادگیری سیستم فقط در پلن‌های استاندارد و حرفه‌ای فعال است.",
        )

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

    # نمونه few-shot برای پرامپت LLM — صف تأیید سوپر ادمین (Q48 ج)
    try:
        from app.config import USER_RECORDS_DB as _URDB
        from app.db import get_conn as _gc
        _c = _gc(_URDB)
        _c.execute(
            "INSERT INTO few_shot_examples (tenant_id, example_type, prompt_text, corrected_text, status, created_by) "
            "VALUES (?,?,?,?, 'pending', ?)",
            (tenant_id, "match",
             f"مفهوم: {concept_label} | نتیجه ادمین: {'موفق' if success else 'ناموفق'}",
             matched, user["username"]),
        )
        _c.commit()
        _c.close()
    except Exception:
        pass  # صف نمونه فرعی است — وزن‌ها اعمال شدند

    w = get_learning_weight(matched, concept_key, tenant_id)
    arrow = "⬆️" if success else "⬇️"
    flash = f"🧠 بازخورد ثبت شد: {matched} — {concept_label} {arrow} وزن فعلی: ×{w:.2f} (نمونه در صف تأیید LLM ثبت شد)"
    return await _tenant_dashboard(request, user, flash=flash)


# ── کراول دستی (فقط tenant نمونه) — state/lease دیتابیس‌محور (چند worker) ──

import asyncio as _asyncio
from app import crawl_state as _crawl_state_mod


def _crawl_background(token: str, tenant_id: int = 0):
    """پس‌زمینه‌ی کراول دستی — lease با توکن گرفته شده؛ پیشرفت در DB"""
    try:
        # داخلِ try: اگر reset_progress بترکد (دیتابیس پر/قفل)، release در
        # finally باید اجرا شود — بیرونِ try، lease تا سررسید TTL گیر می‌کرد
        _crawl_state_mod.reset_progress()

        def _progress(done: int, total: int, current: str, free_total: int):
            # heartbeat: تمدید lease خودمان در هر صفحه تا کِند شدن همپوشانی ندهد
            _crawl_state_mod.renew(_crawl_state_mod.CRAWL_LEASE, token,
                                   _crawl_state_mod.CRAWL_LEASE_TTL)
            _crawl_state_mod.update_progress(done=done, total=total,
                                             current=current, free_total=free_total)

        crawl_available_slots(progress_cb=_progress, tenant_id=tenant_id)
        _crawl_state_mod.update_progress(finished=True)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("manual crawl error: %s", e)
        _crawl_state_mod.update_progress(error=str(e)[:200], finished=True)
        try:
            from app.crawler import record_crawl_run
            record_crawl_run(0, 0, 0, ok=False, error=str(e))
        except Exception:
            pass
    finally:
        _crawl_state_mod.release(_crawl_state_mod.CRAWL_LEASE, token)


@router.get("/crawl/status")
async def crawl_status(request: Request):
    """وضعیت زنده کراول دستی برای polling صفحه داشبورد"""
    from fastapi.responses import JSONResponse
    user = _current_user(request)
    if not user:
        return JSONResponse({"running": False, "login": True}, status_code=401)
    if user["role"] == "super_admin":
        return JSONResponse({"running": False}, status_code=403)
    tenant = _tenant_of(user)
    if tenant["slug"] != "demo":
        return JSONResponse({"running": False}, status_code=403)
    status = await run_in_threadpool(_crawl_state_mod.get_status)
    return JSONResponse({k: status[k] for k in
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
    if tenant["slug"] != "demo":
        return await _tenant_dashboard(request, user, flash="کراول نوبت فقط برای مرکز نمونه فعال است.")
    token = await run_in_threadpool(
        _crawl_state_mod.try_acquire, _crawl_state_mod.CRAWL_LEASE,
        _crawl_state_mod.CRAWL_LEASE_TTL,
    )
    if not token:
        return await _tenant_dashboard(request, user,
            flash="⏳ کراول قبلی هنوز در حال اجراست (حدود ۵ دقیقه طول می‌کشد). چند دقیقه دیگر صفحه را رفرش کنید.")
    loop = _asyncio.get_running_loop()
    loop.run_in_executor(None, _crawl_background, token, tenant["id"])
    return await _tenant_dashboard(request, user,
        flash="⏳ کراول نوبت‌ها در پس‌زمینه آغاز شد (۸۶ صفحه سایت — حدود ۵ دقیقه). چند دقیقه دیگر رفرش کنید تا آمار جدید را ببینید.")


# ── تغییر رمز خود ──

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


# ── مدیریت شعبه‌ها (منبع حقیقت: گزینه‌های استپ branch در فلو) ──

_BRANCH_NEUTRAL = "اهمیتی ندارد"


def _branch_step_of(flow: dict) -> dict | None:
    for s in flow.get("steps") or []:
        if isinstance(s, dict) and s.get("key") == "branch" and s.get("type") == "choice":
            return s
    return None


def _editable_branches(options) -> list[str]:
    """گزینه‌های قابل ویرایش — «اهمیتی ندارد» حذف (ثابت و همیشه موجود)."""
    return [str(o).strip() for o in (options or [])
            if str(o).strip() and str(o).strip() != _BRANCH_NEUTRAL]


def _validate_branch_name(name: str, existing: list[str], exclude_index: int = -1) -> str | None:
    name = name.strip()
    if not name:
        return "نام شعبه خالی است."
    if name == _BRANCH_NEUTRAL:
        return f"«{_BRANCH_NEUTRAL}» گزینهٔ ثابت است و قابل ثبت نیست."
    if len(name) > 60:
        return "نام شعبه حداکثر ۶۰ کاراکتر باشد."
    for i, o in enumerate(existing):
        if i != exclude_index and o == name:
            return "این شعبه قبلاً ثبت شده است."
    return None


def _save_branch_options(tenant_id: int, flow: dict, step: dict, editable: list[str]) -> None:
    opts = list(editable)
    # «اهمیتی ندارد» اگر بود آخر می‌ماند؛ اگر فلوی دستی حذفش کرده بود، تحمیل نمی‌شود
    if _BRANCH_NEUTRAL in [str(o).strip() for o in (step.get("options") or [])]:
        opts.append(_BRANCH_NEUTRAL)
    step["options"] = opts
    tenants_mod.save_flow_config(tenant_id, flow)


async def _branches_view(request: Request, user: dict,
                          flash: str | None = None, err: str | None = None):
    tenant = _tenant_of(user)
    flow = tenant["flow_config"]
    step = _branch_step_of(flow)
    branches = _editable_branches(step.get("options") if step else None)
    return _render(request, "admin_branches.html", {
        "user": user, "flash": flash, "err": err,
        "has_step": step is not None, "branches": branches,
    })


def _branches_auth(request: Request):
    """مشترک همهٔ مسیرهای شعبه: ورود (ریدایرکت) + فقط نقش admin (403).
    خروجی: dict کاربر، یا RedirectResponse اگر وارد نشده."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    return user


@router.get("/branches", response_class=HTMLResponse)
async def branches_page(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] == "super_admin":
        raise HTTPException(status_code=403, detail="این صفحه مخصوص مشتریان است.")
    return await _branches_view(request, user)


@router.post("/branches/create-step")
async def branch_create_step(request: Request):
    user = _branches_auth(request)
    if isinstance(user, RedirectResponse):
        return user
    tenant = _tenant_of(user)
    flow = tenant["flow_config"]
    if _branch_step_of(flow):
        return await _branches_view(request, user, err="سؤال شعبه از قبل در فلو هست.")
    flow["steps"] = list(flow.get("steps") or []) + [{
        "key": "branch", "type": "choice",
        "options": [_BRANCH_NEUTRAL],
        "label": "کدام شعبه مرکز مشاوره برای شما مناسب‌تر است؟",
        "source": "branch",
    }]
    tenants_mod.save_flow_config(tenant["id"], flow)
    return await _branches_view(request, user, flash="✅ سؤال شعبه به فلو اضافه شد — حالا شعبه‌ها را ثبت کنید.")


@router.post("/branches")
async def branch_add(request: Request, name: str = Form("")):
    user = _branches_auth(request)
    if isinstance(user, RedirectResponse):
        return user
    tenant = _tenant_of(user)
    flow = tenant["flow_config"]
    step = _branch_step_of(flow)
    if not step:
        return await _branches_view(request, user, err="سؤال شعبه در فلو نیست — ابتدا آن را بسازید.")
    editable = _editable_branches(step.get("options"))
    err = _validate_branch_name(name, editable)
    if err:
        return await _branches_view(request, user, err=err)
    editable.append(name.strip())
    _save_branch_options(tenant["id"], flow, step, editable)
    return await _branches_view(request, user, flash=f"✅ شعبهٔ «{name.strip()}» اضافه شد.")


@router.post("/branches/update")
async def branch_update(request: Request, index: int = Form(-1), name: str = Form("")):
    user = _branches_auth(request)
    if isinstance(user, RedirectResponse):
        return user
    tenant = _tenant_of(user)
    flow = tenant["flow_config"]
    step = _branch_step_of(flow)
    if not step:
        return await _branches_view(request, user, err="سؤال شعبه در فلو نیست.")
    editable = _editable_branches(step.get("options"))
    if index < 0 or index >= len(editable):
        return await _branches_view(request, user, err="ردیف نامعتبر است.")
    err = _validate_branch_name(name, editable, exclude_index=index)
    if err:
        return await _branches_view(request, user, err=err)
    old = editable[index]
    editable[index] = name.strip()
    _save_branch_options(tenant["id"], flow, step, editable)
    return await _branches_view(request, user, flash=f"✅ «{old}» به «{name.strip()}» تغییر یافت.")


@router.post("/branches/delete")
async def branch_delete(request: Request, index: int = Form(-1)):
    user = _branches_auth(request)
    if isinstance(user, RedirectResponse):
        return user
    tenant = _tenant_of(user)
    flow = tenant["flow_config"]
    step = _branch_step_of(flow)
    if not step:
        return await _branches_view(request, user, err="سؤال شعبه در فلو نیست.")
    editable = _editable_branches(step.get("options"))
    if index < 0 or index >= len(editable):
        return await _branches_view(request, user, err="ردیف نامعتبر است.")
    removed = editable.pop(index)
    _save_branch_options(tenant["id"], flow, step, editable)
    return await _branches_view(request, user, flash=f"✅ شعبهٔ «{removed}» حذف شد.")


