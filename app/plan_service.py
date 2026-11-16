# -*- coding: utf-8 -*-
"""سیستم پلن و سقف — الگوی قابل استفاده مجدد + تخصیص به tenant (تصمیمات Q10/Q13/Q14/Q19/Q20/Q24/Q33/Q40)
- سقف = -1 یعنی نامحدود؛ >=0 سقف مطلق
- پلن ضعیف (weak): فقط موتور قاعده‌ای، بدون LLM، سقف پیام ندارد (Q21)
- بدون پلن = قفل (Q40 الف)؛ عندالصفر قفل کامل + پیام به کاربر (Q19)
- تغییر پلن = ریست شمارنده‌ها + ثبت تاریخچه (Q33)"""
import logging
import sqlite3
from contextvars import ContextVar
from datetime import datetime, timedelta

from app.config import USER_RECORDS_DB
from app.db import get_conn
from app import encryption_service, audit_service

logger = logging.getLogger(__name__)

MODE_WEAK = "weak"
MODE_MEDIUM = "medium"
MODE_ADVANCED = "advanced"
MODES = (MODE_WEAK, MODE_MEDIUM, MODE_ADVANCED)
MODE_LABELS = {
    MODE_WEAK: "ضعیف — موتور قاعده‌ای (بدون AI)",
    MODE_MEDIUM: "متوسط — فقط LLM",
    MODE_ADVANCED: "پیشرفته — LLM با پشتیبان قاعده‌ای",
}
UNLIMITED = -1

# چهار ردهٔ جدول قیمت. «حرفه‌ای» و «کامل» هر دو MODE_ADVANCED هستند، پس mode
# به‌تنهایی فرقشان را درنمی‌آورد — ردهٔ تجاری جداگانه روی خودِ پلن نگهداری
# می‌شود و قابلیت‌های فقط-«کامل» (گزارش دوره‌ای) از همین خوانده می‌شوند.
TIER_BASE = "base"
TIER_STANDARD = "standard"
TIER_PRO = "pro"
TIER_FULL = "full"
TIERS = (TIER_BASE, TIER_STANDARD, TIER_PRO, TIER_FULL)
TIER_LABELS = {
    TIER_BASE: "پایه — بدون هوش مصنوعی",
    TIER_STANDARD: "استاندارد — مدل توسط ما",
    TIER_PRO: "حرفه‌ای — دیپ‌سیک",
    TIER_FULL: "کامل — مدل دلخواه مشتری",
}


def tier_for(mode: str, message_quota: int) -> str:
    """ردهٔ پیش‌فرض از mode و سقف — همان چیزی که مهاجرت ۶ برای پلن‌های
    موجود ثبت کرده. فرم پلن این را پیشنهاد می‌دهد و سوپرادمین می‌تواند عوضش کند."""
    if mode == MODE_WEAK:
        return TIER_BASE
    if mode == MODE_MEDIUM:
        return TIER_STANDARD
    return TIER_FULL if message_quota == UNLIMITED else TIER_PRO


DEFAULT_LOCK_MESSAGE = (
    "ظرفیت پذیرش آنلاین این مرکز تکمیل شده است. "
    "لطفاً برای هماهنگی با مرکز تماس بگیرید."
)


# آیا در این request پیامی با دخالت AI پردازش شده؟ (شمارندهٔ «پیام هوشمند»)
# ContextVar چون روت‌های def با run_in_threadpool اجرا می‌شوند و هر request
# context کپی‌شدهٔ جدا دارد؛ threading.local در استفاده‌مجدد thread نشت می‌کند.
_AI_PARTICIPATED: ContextVar[bool] = ContextVar("ai_participated", default=False)


def mark_ai_participation() -> None:
    """فراخوانی LLM را علامت می‌زند — قبل از صدا تا خطا هم شمرده شود (Q43)."""
    _AI_PARTICIPATED.set(True)


def take_ai_participation() -> bool:
    """آیا این request مشارکت AI داشته؟ مقدار را صفر می‌کند (خواندن یک‌باره)."""
    v = _AI_PARTICIPATED.get()
    _AI_PARTICIPATED.set(False)
    return v


def _plan_row(row) -> dict:
    d = {
        "id": row[0], "name": row[1], "mode": row[2], "model": row[3],
        "base_url": row[4], "api_key_encrypted": row[5],
        "message_quota": row[6], "introduction_quota": row[7],
        "active": row[8], "created_at": row[9],
    }
    # plans.* با SELECT * خوانده می‌شود؛ ستون tier فقط بعد از مهاجرت ۵ هست
    d["tier"] = row[10] if len(row) > 10 and row[10] else tier_for(d["mode"], d["message_quota"])
    return d


def get_plan(plan_id: int) -> dict | None:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    return _plan_row(row) if row else None


def list_plans(include_inactive: bool = True) -> list[dict]:
    conn = get_conn(USER_RECORDS_DB)
    q = "SELECT * FROM plans"
    if not include_inactive:
        q += " WHERE active=1"
    q += " ORDER BY id DESC"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [_plan_row(r) for r in rows]


def _validate_plan_fields(mode: str, name: str, model: str, base_url: str, api_key: str):
    if mode not in MODES:
        raise ValueError("نوع پلن نامعتبر است.")
    if not (name or "").strip():
        raise ValueError("نام پلن الزامی است.")
    if mode != MODE_WEAK:
        if not (base_url or "").strip():
            raise ValueError("برای پلن LLM‌دار، base_url الزامی است.")
        if not (model or "").strip():
            raise ValueError("برای پلن LLM‌دار، نام مدل الزامی است.")
        if not (api_key or "").strip():
            raise ValueError("برای پلن LLM‌دار، API key الزامی است.")


def _validate_tier(tier: str) -> str:
    tier = (tier or "").strip()
    if tier and tier not in TIERS:
        raise ValueError("ردهٔ پلن نامعتبر است.")
    return tier


# هر ردهٔ تجاری فقط روی یک mode معنا دارد. فرمِ پلن دو select مستقل دارد،
# پس بدون این چهار می‌شد پلنی ساخت که مثلاً mode=weak ولی tier=full —
# یعنی مشتریِ بدون AI با پلنِ کامل. بعضی گیت‌ها tier می‌خوانند و بعضی
# mode، پس چنین ناسازگاری یعنی قابلیت‌های نامسویِ فروخته‌شده.
_TIER_MODE = {
    TIER_BASE: MODE_WEAK,
    TIER_STANDARD: MODE_MEDIUM,
    TIER_PRO: MODE_ADVANCED,
    TIER_FULL: MODE_ADVANCED,
}


def _validate_tier_matches_mode(tier: str, mode: str) -> None:
    if tier and _TIER_MODE.get(tier) != mode:
        expected = "، ".join(
            lbl for t, m in _TIER_MODE.items() if m == mode for lbl in (TIER_LABELS[t],)
        )
        raise ValueError(
            f"ردهٔ «{TIER_LABELS[tier]}» با حالتِ «{MODE_LABELS.get(mode, mode)}» "
            f"سازگار نیست — این حالت فقط با ردهٔ {expected} کار می‌کند."
        )


def create_plan(name: str, mode: str, model: str = "", base_url: str = "",
                api_key: str = "", message_quota: int = UNLIMITED,
                introduction_quota: int = UNLIMITED, created_by: str = "",
                tier: str = "") -> int:
    _validate_plan_fields(mode, name, model, base_url, api_key)
    tier = _validate_tier(tier) or tier_for(mode, int(message_quota))
    _validate_tier_matches_mode(tier, mode)
    enc = encryption_service.encrypt_str((api_key or "").strip()) if mode != MODE_WEAK else ""
    conn = get_conn(USER_RECORDS_DB)
    try:
        cur = conn.execute(
            "INSERT INTO plans (name, mode, model, base_url, api_key_encrypted, message_quota, introduction_quota, tier) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name.strip()[:120], mode, (model or "").strip()[:120],
             (base_url or "").strip()[:300], enc, int(message_quota), int(introduction_quota), tier),
        )
        conn.commit()
        plan_id = cur.lastrowid
    finally:
        conn.close()
    audit_service.log(created_by, "plan.create",
                      {"plan_id": plan_id, "name": name.strip(), "mode": mode, "tier": tier})
    return plan_id


def update_plan(plan_id: int, name: str, mode: str, model: str = "", base_url: str = "",
                api_key: str = "", message_quota: int = UNLIMITED,
                introduction_quota: int = UNLIMITED, updated_by: str = "",
                tier: str = ""):
    """api_key خالی = بدون تغییر کلید ذخیره‌شده"""
    existing = get_plan(plan_id)
    if not existing:
        raise ValueError("پلن یافت نشد.")
    _validate_plan_fields(mode, name, model, base_url, api_key or existing["api_key_encrypted"])
    tier = _validate_tier(tier) or existing["tier"]
    _validate_tier_matches_mode(tier, mode)
    if mode == MODE_WEAK:
        enc = ""
    elif (api_key or "").strip():
        enc = encryption_service.encrypt_str(api_key.strip())
    else:
        enc = existing["api_key_encrypted"]
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute(
            "UPDATE plans SET name=?, mode=?, model=?, base_url=?, api_key_encrypted=?, "
            "message_quota=?, introduction_quota=?, tier=? WHERE id=?",
            (name.strip()[:120], mode, (model or "").strip()[:120],
             (base_url or "").strip()[:300], enc, int(message_quota), int(introduction_quota),
             tier, plan_id),
        )
        conn.commit()
    finally:
        conn.close()
    audit_service.log(updated_by, "plan.update",
                      {"plan_id": plan_id, "name": name.strip(), "mode": mode, "tier": tier})


def get_decrypted_api_key(plan_id: int) -> str:
    plan = get_plan(plan_id)
    if not plan:
        return ""
    return encryption_service.decrypt_str(plan["api_key_encrypted"])


# ── پیکربندی LLM اختصاصیِ هر مشتری (مهاجرت ۱۰) ──

def set_tenant_llm(tenant_id: int, base_url: str = "", model: str = "",
                   api_key: str = "", by: str = ""):
    """درگاه/کلید/مدلِ همین مشتری — نه پلن. برخلاف ویرایش پلن، فقط همین مشتری.

    api_key خالی = بدون تغییر کلیدِ ذخیره‌شده (مثل update_plan).
    base_url/model خالی = برگشت به ارث از پلن.
    هر سه خالی = کاملاً ارث‌از-پلن؛ کلید قبلیِ مشتری در آن حالت پاک می‌شود."""
    url = (base_url or "").strip()[:300]
    mdl = (model or "").strip()[:120]
    raw_key = (api_key or "").strip()
    conn = get_conn(USER_RECORDS_DB)
    try:
        row = conn.execute(
            "SELECT tp.llm_api_key_encrypted, p.mode FROM tenant_plans tp "
            "JOIN plans p ON p.id=tp.plan_id WHERE tp.tenant_id=?",
            (tenant_id,),
        ).fetchone()
        if not row:
            raise ValueError("این مشتری پلنی ندارد — اول پلن تخصیص دهید.")
        if row[1] == MODE_WEAK:
            raise ValueError("پلن این مشتری «ضعیف» است و هیچ فراخوانی LLM ندارد.")
        if raw_key:
            enc = encryption_service.encrypt_str(raw_key)
        elif url or mdl:
            enc = row[0] or ""  # کلید عوض نشده، فقط درگاه/مدل تغییر کرد
        else:
            enc = ""  # بازگشت کامل به ارث از پلن
        conn.execute(
            "UPDATE tenant_plans SET llm_base_url=?, llm_model=?, llm_api_key_encrypted=? "
            "WHERE tenant_id=?",
            (url, mdl, enc, tenant_id),
        )
        conn.commit()
    finally:
        conn.close()
    audit_service.log(by, "tenant.llm", {
        "tenant_id": tenant_id, "base_url": url, "model": mdl,
        "has_key": bool(enc),
    })


def get_tenant_api_key(tenant_id: int) -> str:
    """کلید اختصاصیِ مشتری؛ خالی یعنی ارث از پلن."""
    state = get_tenant_plan_state(tenant_id)
    if not state:
        return ""
    enc = (state.get("tenant") or {}).get("llm_api_key_encrypted") or ""
    if not enc:
        return ""
    return encryption_service.decrypt_str(enc)


def get_tenant_llm_config(tenant_id: int) -> dict:
    """پیکربندی فعلیِ LLM همین مشتری برای پرکردن فرم سوپرادمین.

    در سه حالت پر می‌شود: کاملِ مشتری، ناقصِ مشتری، یا خالی = ارث از پلن —
    تا سوپرادمین ببیند دقیقاً چه چیزی در حال استفاده است."""
    state = get_tenant_plan_state(tenant_id)
    if not state:
        return {"base_url": "", "model": "", "has_key": False,
                "source": "none", "plan_base_url": "", "plan_model": ""}
    own = state.get("tenant") or {}
    plan = state["plan"]
    own_url, own_model = own.get("llm_base_url", ""), own.get("llm_model", "")
    own_enc = own.get("llm_api_key_encrypted", "")
    if own_url and own_model and own_enc:
        source = "tenant"
    elif own_url or own_model or own_enc:
        source = "partial"
    else:
        source = "plan"
    return {
        "base_url": own_url if source == "tenant" else "",
        "model": own_model if source == "tenant" else "",
        "has_key": bool(own_enc),
        "source": source,
        "plan_base_url": (plan["base_url"] or "").strip(),
        "plan_model": (plan["model"] or "").strip(),
    }


def renew_quotas(tenant_id: int, by: str = ""):
    """تمدید سقف‌های ماهانه: شمارنده‌ها صفر، بدون تعویض پلن.

    تفاوتِ کلیدی با assign_plan: سقف اختصاصیِ خریده‌شدهٔ مشتری و اعتبارِ
    بسته‌های اضافه دست‌نخورده می‌مانند — تمدید نباید چیزی را بشکند."""
    conn = get_conn(USER_RECORDS_DB)
    try:
        exists = conn.execute("SELECT 1 FROM tenant_plans WHERE tenant_id=?",
                              (tenant_id,)).fetchone()
        if not exists:
            raise ValueError("این مشتری پلنی ندارد.")
        conn.execute(
            # کلید ماهِ جاری، نه خالی: خالی یعنی «ریستِ اولِ job اجرا نشده» و
            # ریستِ تنبل را غیرفعال می‌کند — مشتریِ تمدیدشده‌ای که سقفش تمام
            # شده و ماه عوض شده (مثلاً سرور خاموش بوده) تا اجرای job قفل می‌ماند
            "UPDATE tenant_plans SET messages_used=0, introductions_used=0, "
            "last_reset_month=? WHERE tenant_id=?",
            (_tehran_month(), tenant_id),
        )
        conn.commit()
    finally:
        conn.close()
    audit_service.log(by, "plan.renew_quotas", {"tenant_id": tenant_id})


def assign_plan(tenant_id: int, plan_id: int, by: str = ""):
    """تخصیص پلن = ریست شمارنده‌ها + ثبت تاریخچه (Q33)"""
    plan = get_plan(plan_id)
    if not plan or not plan["active"]:
        raise ValueError("پلن معتبر/فعال نیست.")
    conn = get_conn(USER_RECORDS_DB)
    try:
        prev = conn.execute(
            "SELECT plan_id, messages_used, introductions_used FROM tenant_plans WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        prev_plan = prev[0] if prev else None
        prev_msgs = prev[1] if prev else 0
        prev_intros = prev[2] if prev else 0
        # سقفِ اختصاصی هم با تخصیص پاک می‌شود: آن مقدار برای پلنِ قبلی
        # («سفارشی»ِ کامل) تنظیم شده بود، نه برای این پلن. بدون این پاک‌سازی،
        # ارتقای مشتری به پلنی با سقفِ بزرگ‌تر بی‌اثر می‌شد — چون سقفِ
        # کوچکِ قبلی همچنان مقدم بود و مشتری زودتر قفل می‌شد.
        conn.execute(
            "INSERT INTO tenant_plans (tenant_id, plan_id, messages_used, introductions_used, assigned_by) "
            "VALUES (?,?,0,0,?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET plan_id=excluded.plan_id, messages_used=0, "
            "introductions_used=0, message_quota_override=NULL, "
            "introduction_quota_override=NULL, "
            "assigned_at=CURRENT_TIMESTAMP, assigned_by=excluded.assigned_by",
            (tenant_id, plan_id, by or ""),
        )
        conn.execute(
            "INSERT INTO plan_history (tenant_id, plan_id, previous_plan_id, messages_used_at_change, "
            "introductions_used_at_change, changed_by) VALUES (?,?,?,?,?,?)",
            (tenant_id, plan_id, prev_plan, prev_msgs, prev_intros, by or ""),
        )
        conn.commit()
    finally:
        conn.close()
    audit_service.log(by, "plan.assign",
                      {"tenant_id": tenant_id, "plan_id": plan_id, "previous_plan_id": prev_plan})


def tenant_has_mode(tenant_id: int, *modes: str) -> bool:
    """آیا پلن tenant جزو این modeهاست؟ برای قابلیت‌هایی که فقط روی پلن‌های LLM‌دار فعال می‌شوند.
    بدون پلن یا پلن غیرفعال → False (قابلیت‌های پیشرفته خاموش می‌مانند، نه باز)."""
    state = get_tenant_plan_state(tenant_id)
    return bool(state and state["plan"]["active"] and state["plan"]["mode"] in modes)


def tenant_has_report(tenant_id: int) -> bool:
    """گزارش دوره‌ای فقط در پلن «کامل».

    «حرفه‌ای» و «کامل» هر دو MODE_ADVANCED هستند، پس mode فرقشان را درنمی‌آورد
    و ردهٔ تجاریِ plans.tier مرجع است. این رده از خودِ پلن خوانده می‌شود، نه از
    سقف پیام — وگرنه یک اشتباه تایپی در فرم سقف، گزارشِ خریده‌شدهٔ مشتری را
    خاموش می‌کرد (این دقیقاً همان اتفاقی بود که این گیتِ قبلی را بد می‌کرد)."""
    state = get_tenant_plan_state(tenant_id)
    if not state:
        return False
    plan = state["plan"]
    return bool(plan["active"] and plan["tier"] == TIER_FULL)


def _refill_if_month_elapsed(conn, tenant_id: int, last_reset: str | None) -> bool:
    """ریست تنبل: اگر ماهِ تهران عوض شده، شمارنده‌های همین مشتری همین‌جا صفر می‌شوند.

    بدون این، تنها راهِ بازشدنِ سقف ماهانه job ساعتی بود — و آن فقط وقتی
    اجرا می‌شد که سرور بالا باشد. مشتری‌ای که شبِ آخر ماه به سقف رسیده بود،
    با ری‌استارت یا خاموشیِ سرور تا روزها قفل می‌ماند؛ یعنی سرویسی که خریده
    بود خاموش می‌شد.

    last_reset خالی = هنوز اولین ریستِ job اجرا نشده؛ آن مورد با job
    می‌ماند (تاریخچهٔ ریستِ اول باید با مقادیرِ واقعِ قبل از ریست ثبت شود
    و کسی که تازه پلن گرفته چیزی برای ازدست‌دادن ندارد).

    خروجی: True یعنی ریست شد."""
    if not last_reset or last_reset == _tehran_month():
        return False
    current = _tehran_month()
    try:
        conn.execute(
            "INSERT INTO plan_history (tenant_id, messages_used_at_change, "
            "introductions_used_at_change, changed_by, month_key) "
            "VALUES (?,(SELECT messages_used FROM tenant_plans WHERE tenant_id=?),"
            "(SELECT introductions_used FROM tenant_plans WHERE tenant_id=?),"
            "'monthly_reset',?)",
            (tenant_id, tenant_id, tenant_id, current),
        )
        conn.execute(
            "UPDATE tenant_plans SET messages_used=0, introductions_used=0, "
            "last_reset_month=? WHERE tenant_id=?", (current, tenant_id),
        )
    except sqlite3.OperationalError:
        # این مسیر روی درخواستِ کاربر است؛ قفلِ writers نباید ویجت را
        # بترکاند. job ساعتی همان کار را دوباره و بی‌نهایت تلاش می‌کند.
        logger.warning("lazy quota refill failed tenant=%s", tenant_id, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    return True


def get_tenant_plan_state(tenant_id: int) -> dict | None:
    """وضعیت تخصیص + پلن عمومی (بدون API key) — برای پنل ادمین و نمایش"""
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT tp.plan_id, tp.messages_used, tp.introductions_used, tp.assigned_at, "
        "p.id, p.name, p.mode, p.model, p.base_url, p.message_quota, p.introduction_quota, p.active, "
        "p.tier, "
        "tp.message_quota_override, tp.introduction_quota_override, "
        "tp.extra_messages, tp.extra_introductions, tp.last_reset_month, "
        "tp.llm_base_url, tp.llm_model, tp.llm_api_key_encrypted "
        "FROM tenant_plans tp JOIN plans p ON p.id = tp.plan_id WHERE tp.tenant_id=?",
        (tenant_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    msg_used, intro_used = row[1], row[2]
    if _refill_if_month_elapsed(conn, tenant_id, row[17]):
        conn.commit()
        msg_used, intro_used = 0, 0
    conn.close()
    plan = {
        "id": row[4], "name": row[5], "mode": row[6], "model": row[7],
        "base_url": row[8], "message_quota": row[9], "introduction_quota": row[10],
        "active": row[11],
        # پلن‌های ساختهٔ پیش از مهاجرت ۵ ممکن است ستون را نداشته باشند
        "tier": (row[12] or "") or tier_for(row[6], row[9]),
    }
    override_msg, override_intro = row[13], row[14]
    extra_msg, extra_intro = (row[15] or 0), (row[16] or 0)
    # سقف مؤثر: override اگر ست شده باشد، وگرنه سقف پلن؛ اعتبار بستهٔ اضافه
    # روی هر دو جمع می‌شود (بستهٔ خریداری‌شده باید تا هر زمانِ دلخواه بماند).
    eff_msg = override_msg if override_msg is not None else plan["message_quota"]
    eff_intro = override_intro if override_intro is not None else plan["introduction_quota"]
    if eff_msg >= 0:
        eff_msg += extra_msg
    if eff_intro >= 0:
        eff_intro += extra_intro
    return {
        "plan_id": row[0], "messages_used": msg_used, "introductions_used": intro_used,
        "assigned_at": row[3],
        "message_quota_override": override_msg,
        "introduction_quota_override": override_intro,
        "extra_messages": extra_msg,
        "extra_introductions": extra_intro,
        "effective_message_quota": eff_msg,
        "effective_introduction_quota": eff_intro,
        "plan": plan,
        # پیکربندی LLM اختصاصیِ همین مشتری (مهاجرت ۱۰). خالی = ارث از پلن؛
        # کلید رمزگذاری‌شده برمی‌گردد، رمزگشایی در get_tenant_api_key انجام می‌شود.
        "tenant": {
            "llm_base_url": (row[18] or "").strip(),
            "llm_model": (row[19] or "").strip(),
            "llm_api_key_encrypted": row[20] or "",
        },
    }


def check_lock(tenant_id: int) -> tuple[bool, str]:
    """خروجی: (قفل؟, دلیل) — دلیل: no_plan | message_quota | intro_quota"""
    state = get_tenant_plan_state(tenant_id)
    if not state:
        return True, "no_plan"
    plan = state["plan"]
    if not plan["active"]:
        return True, "no_plan"
    if (state["effective_introduction_quota"] >= 0
            and state["introductions_used"] >= state["effective_introduction_quota"]):
        return True, "intro_quota"
    # سقف پیام فقط برای پلن‌های LLM‌دار (Q21/Q24)
    if (plan["mode"] != MODE_WEAK and state["effective_message_quota"] >= 0
            and state["messages_used"] >= state["effective_message_quota"]):
        return True, "message_quota"
    return False, ""


def lock_message_for(tenant_id: int) -> str:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT lock_message FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    return DEFAULT_LOCK_MESSAGE


def set_lock_message(tenant_id: int, message: str, by: str = ""):
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute("UPDATE tenants SET lock_message=? WHERE id=?",
                     ((message or "").strip()[:500], tenant_id))
        conn.commit()
    finally:
        conn.close()
    audit_service.log(by, "tenant.lock_message",
                      {"tenant_id": tenant_id, "message": (message or "")[:80]})


def increment_messages(tenant_id: int) -> bool:
    """شمارش اتمیک پیام LLM (BEGIN IMMEDIATE — بدون رد سقف زیر چند-worker).
    weak/بدون پلن/سقف‌پر → False و شمارش نمی‌شود. خطا هم شمارش می‌شود (Q43)."""
    conn = get_conn(USER_RECORDS_DB)
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT p.mode, p.message_quota, tp.messages_used, "
            "tp.message_quota_override, tp.extra_messages, tp.last_reset_month "
            "FROM tenant_plans tp JOIN plans p ON p.id=tp.plan_id WHERE tp.tenant_id=?",
            (tenant_id,),
        ).fetchone()
        if row and row[0] != MODE_WEAK and _refill_if_month_elapsed(conn, tenant_id, row[5]):
            # ماه عوض شده و این مشتری هنوز ریست نشده — شمارنده را صفر کن
            # تا مصرفِ این نوبت روی ماهِ قبل ننشیند (job ساعتی ممکن است هنوز
            # نرسیده باشد، مخصوصاً بعد از ری‌استارت)
            row = conn.execute(
                "SELECT p.mode, p.message_quota, tp.messages_used, "
                "tp.message_quota_override, tp.extra_messages, tp.last_reset_month "
                "FROM tenant_plans tp JOIN plans p ON p.id=tp.plan_id WHERE tp.tenant_id=?",
                (tenant_id,),
            ).fetchone()
        if not row or row[0] == MODE_WEAK:
            conn.commit(); committed = True
            return False
        _mode, plan_quota, used, override, extra = row[0], row[1], row[2], row[3], row[4]
        quota = override if override is not None else plan_quota
        if quota >= 0:
            quota += extra or 0
        if quota >= 0 and used >= quota:
            conn.commit(); committed = True
            return False
        conn.execute(
            "UPDATE tenant_plans SET messages_used = messages_used + 1 WHERE tenant_id=?",
            (tenant_id,),
        )
        conn.commit(); committed = True
        return True
    finally:
        if not committed:
            try:
                conn.rollback()
            except Exception:
                pass
        conn.close()


def increment_introductions(tenant_id: int) -> bool:
    """مصرف اتمیک ۱ واحد سقف معرفی — قبل از نمایش معرفی صدا زده می‌شود
    تا دو گفتگوی همزمان از سقف پولی رد نشوند (Q13/Q19).
    خروجی: True=مصرف شد؛ False=بدون پلن/سقف‌پر (نمایش نده)"""
    conn = get_conn(USER_RECORDS_DB)
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT p.introduction_quota, tp.introductions_used, "
            "tp.introduction_quota_override, tp.extra_introductions, tp.last_reset_month "
            "FROM tenant_plans tp JOIN plans p ON p.id=tp.plan_id WHERE tp.tenant_id=?",
            (tenant_id,),
        ).fetchone()
        if row and _refill_if_month_elapsed(conn, tenant_id, row[4]):
            row = conn.execute(
                "SELECT p.introduction_quota, tp.introductions_used, "
                "tp.introduction_quota_override, tp.extra_introductions, tp.last_reset_month "
                "FROM tenant_plans tp JOIN plans p ON p.id=tp.plan_id WHERE tp.tenant_id=?",
                (tenant_id,),
            ).fetchone()
        if not row:
            conn.commit(); committed = True
            return False
        plan_quota, used, override, extra = row[0], row[1], row[2], row[3]
        quota = override if override is not None else plan_quota
        if quota >= 0:
            quota += extra or 0
        if quota >= 0 and used >= quota:
            conn.commit(); committed = True
            return False
        conn.execute(
            "UPDATE tenant_plans SET introductions_used = introductions_used + 1 WHERE tenant_id=?",
            (tenant_id,),
        )
        conn.commit(); committed = True
        logger.info("introduction consumed for tenant %s (%s/%s)", tenant_id, used + 1, quota)
        return True
    finally:
        if not committed:
            try:
                conn.rollback()
            except Exception:
                pass
        conn.close()


def month_key(created_at: str | None) -> str:
    """کلید ماه به وقت تهران (UTC+3:30) از روی timestamp — 'YYYY-MM'.

    timestampهای SQLite با datetime('now') به وقت UTC ثبت می‌شوند؛
    ماه‌های مرزی باید به وقت تهران شمرده شوند وگرنه شب اول هر ماه،
    مصرف ۳۱ روز گذشته زودتر از موعد ریست می‌شود."""
    if not created_at:
        return ""
    try:
        dt = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""
    return (dt + timedelta(minutes=210)).strftime("%Y-%m")


def reset_expired_quotas(by: str = "system"):
    """ریست ماهانهٔ شمارنده‌ها — «/ ماه» در جدول قیمت بدون این کار یعنی «برای همیشه» (Q19).

    هر tenant فقط یک‌بار در ماه ریست می‌شود: کلید ماه در
    tenant_plans.last_reset_month نگهداری می‌شود، نه در plan_history —
    چون plan_history هم تخصیص پلن را ثبت می‌کند و آن نباید جلوی ریست
    ماه بعد را بگیرد.

    override و اعتبارِ بستهٔ اضافه دست‌نخورده می‌مانند: این‌ها اختیارِ خریداری‌شده
    اند، نه مصرفِ ماهِ جاری. (پاک‌کردنشان یعنی «میزانی که خریدی» بی‌صدا از بین
    برود.)"""
    conn = get_conn(USER_RECORDS_DB)
    reset = []
    try:
        current = _tehran_month()
        rows = conn.execute(
            "SELECT tenant_id, messages_used, introductions_used, last_reset_month "
            "FROM tenant_plans"
        ).fetchall()
        for tenant_id, msg_used, intro_used, last_reset in rows:
            if last_reset == current:
                continue  # همین ماه ریست شده
            conn.execute(
                "INSERT INTO plan_history (tenant_id, messages_used_at_change, "
                "introductions_used_at_change, changed_by, month_key) "
                "VALUES (?,?,?,?,?)",
                (tenant_id, msg_used, intro_used, "monthly_reset", current),
            )
            conn.execute(
                "UPDATE tenant_plans SET messages_used=0, introductions_used=0, "
                "last_reset_month=? WHERE tenant_id=?", (current, tenant_id),
            )
            reset.append(tenant_id)
        conn.commit()
    finally:
        conn.close()
    if reset:
        audit_service.log(by, "plan.monthly_reset", {"tenants": reset})
    return reset


def _tehran_month() -> str:
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    return (_dt.now(_tz(_td(minutes=210)))).strftime("%Y-%m")


def reset_quotas(tenant_id: int, by: str = ""):
    """ریست دستی شمارنده‌ها توسط سوپرادمین (Q19) — خودِ پلن عوض نمی‌شود"""
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute(
            "UPDATE tenant_plans SET messages_used=0, introductions_used=0 WHERE tenant_id=?",
            (tenant_id,),
        )
        conn.commit()
    finally:
        conn.close()
    audit_service.log(by, "plan.reset_quotas", {"tenant_id": tenant_id})


def get_usage_overview() -> dict:
    """همه tenantها با پلن/مصرف — برای جدول سوپرادمین (Q30)"""
    conn = get_conn(USER_RECORDS_DB)
    rows = conn.execute("""
        SELECT t.id, p.name, p.mode, p.message_quota, p.introduction_quota,
               tp.messages_used, tp.introductions_used, tp.assigned_at, p.tier,
               tp.message_quota_override, tp.introduction_quota_override,
               tp.extra_messages, tp.extra_introductions,
               tp.llm_base_url, tp.llm_model, tp.llm_api_key_encrypted,
               p.base_url, p.model, tp.plan_id
        FROM tenants t
        LEFT JOIN tenant_plans tp ON tp.tenant_id = t.id
        LEFT JOIN plans p ON p.id = tp.plan_id
    """).fetchall()
    conn.close()
    out = {}
    for r in rows:
        if r[1] is None:
            out[r[0]] = None  # بدون پلن
        else:
            eff_msg = r[9] if r[9] is not None else r[3]
            eff_intro = r[10] if r[10] is not None else r[4]
            if eff_msg >= 0:
                eff_msg += r[11] or 0
            if eff_intro >= 0:
                eff_intro += r[12] or 0
            own_url, own_model, own_enc = (r[13] or ""), (r[14] or ""), (r[15] or "")
            # فعال فقط وقتی هر سه کامل است، وگرنه ارث از پلن — همان قاعدهٔ
            # get_tenant_llm؛ نیمه‌کاره نباید در کارت مشتری نمایش داده شود.
            if own_url and own_model and own_enc:
                llm = {"base_url": own_url, "model": own_model,
                       "has_key": True, "source": "tenant"}
            else:
                llm = {"base_url": (r[16] or "").strip(),
                       "model": (r[17] or "").strip(),
                       "has_key": False, "source": "plan"}
            out[r[0]] = {
                "plan_name": r[1], "mode": r[2],
                "message_quota": r[3], "introduction_quota": r[4],
                "messages_used": r[5], "introductions_used": r[6],
                "assigned_at": r[7],
                "tier": (r[8] or "") or tier_for(r[2], r[3]),
                "message_quota_override": r[9],
                "introduction_quota_override": r[10],
                "extra_messages": r[11] or 0,
                "extra_introductions": r[12] or 0,
                "effective_message_quota": eff_msg,
                "effective_introduction_quota": eff_intro,
                "llm": llm,
                "plan_id": r[18],
            }
    return out


def quota_label(used: int, quota: int) -> str:
    if quota < 0:
        return f"{used} / نامحدود"
    return f"{used} / {quota}"


_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def rial_label(rials: int) -> str:
    """مبلغ به تومان با رقم فارسی و جداکنندهٔ هزارگان — همهٔ قیمت‌های
    جدول قیمت به تومان‌اند، دیتابیس ریال."""
    return f"{rials // 10:,}".translate(_FA_DIGITS).replace(",", "٬") + " تومان"


# ── سقف اختصاصیِ مشتری + بستهٔ اضافه ──

# سقفِ عملی برای سقف/تعداد/مبلغ. SQLite ستون‌های INTEGER را تا ۲^۶۳-۱ نگه
# می‌دارد و عددِ بزرگ‌تر با OverflowError می‌ترکد — که در روت‌ها گرفته نمی‌شود
# و ۵۰۰ می‌دهد. سقفِ ۲ میلیارد برای «تعداد پیام» بیش از حدِ کافی است.
MAX_INT = 2_147_483_647


def _clean_quota(raw: str | None) -> int | None:
    """فرم خالی = «از پلن ارث ببر» → None. غیرخالی = سقفِ اختصاصی."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        v = int(raw)
    except ValueError:
        raise ValueError("سقف باید عدد صحیح باشد یا خالی (خالی = ارث از پلن).")
    if v < -1:
        raise ValueError("سقف نمی‌تواند کمتر از -1 باشد.")
    if v > MAX_INT:
        raise ValueError("سقف بیش از حد بزرگ است.")
    return v


def set_quota_override(tenant_id: int, message_quota: str | None = None,
                        introduction_quota: str | None = None, by: str = ""):
    """سقف اختصاصیِ یک مشتری — «سقف کاملاً سفارشی» جدول قیمت.

    برخلاف ویرایش پلن، این فقط روی همین مشتری اثر دارد: ویرایش یک پلنِ
    مشترک، retroactively روی بقیهٔ مشتری‌های آن پلن هم اثر می‌گذاشت.
    خالی گذاشتن هر فیلد = برگشت به سقف پلن."""
    msg = _clean_quota(message_quota)
    intro = _clean_quota(introduction_quota)
    if message_quota is None and introduction_quota is None:
        raise ValueError("حداقل یکی از سقف‌ها را وارد کنید.")
    conn = get_conn(USER_RECORDS_DB)
    try:
        exists = conn.execute("SELECT 1 FROM tenant_plans WHERE tenant_id=?", (tenant_id,)).fetchone()
        if not exists:
            raise ValueError("این مشتری پلنی ندارد — اول پلن تخصیص دهید.")
        if message_quota is not None:
            conn.execute("UPDATE tenant_plans SET message_quota_override=? WHERE tenant_id=?",
                         (msg, tenant_id))
        if introduction_quota is not None:
            conn.execute("UPDATE tenant_plans SET introduction_quota_override=? WHERE tenant_id=?",
                         (intro, tenant_id))
        conn.commit()
    finally:
        conn.close()
    audit_service.log(by, "plan.quota_override",
                      {"tenant_id": tenant_id, "message_quota": msg, "introduction_quota": intro})


def list_packages(include_inactive: bool = False) -> list[dict]:
    conn = get_conn(USER_RECORDS_DB)
    q = "SELECT * FROM topup_packages"
    if not include_inactive:
        q += " WHERE active=1"
    q += " ORDER BY price_rials, id"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "extra_messages": r[2], "extra_introductions": r[3],
         "price_rials": r[4], "active": r[5], "created_at": r[6]}
        for r in rows
    ]


def create_package(name: str, extra_messages: int, extra_introductions: int,
                   price_rials: int, by: str = "") -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("نام بسته الزامی است.")
    if extra_messages < 0 or extra_introductions < 0:
        raise ValueError("تعداد پیام/معرفی نمی‌تواند منفی باشد.")
    if extra_messages == 0 and extra_introductions == 0:
        raise ValueError("بسته باید حداقل یک پیام یا یک معرفی اضافه کند.")
    if extra_messages > MAX_INT or extra_introductions > MAX_INT:
        raise ValueError("تعداد پیام/معرفی بیش از حد بزرگ است.")
    if price_rials < 0:
        raise ValueError("مبلغ نمی‌تواند منفی باشد.")
    if price_rials > MAX_INT:
        raise ValueError("مبلغ بیش از حد بزرگ است.")
    conn = get_conn(USER_RECORDS_DB)
    try:
        cur = conn.execute(
            "INSERT INTO topup_packages (name, extra_messages, extra_introductions, price_rials) "
            "VALUES (?,?,?,?)",
            (name[:120], int(extra_messages), int(extra_introductions), int(price_rials)),
        )
        conn.commit()
        pkg_id = cur.lastrowid
    finally:
        conn.close()
    audit_service.log(by, "topup.package.create",
                      {"package_id": pkg_id, "name": name, "price_rials": price_rials})
    return pkg_id


def set_package_active(package_id: int, active: bool, by: str = ""):
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute("UPDATE topup_packages SET active=? WHERE id=?", (1 if active else 0, package_id))
        conn.commit()
    finally:
        conn.close()
    audit_service.log(by, "topup.package.active", {"package_id": package_id, "active": active})


def request_topup(tenant_id: int, package_id: int, by: str = "") -> int:
    """مشتری بسته را می‌خرد. مبلغ و تعداد همان لحظه اسنپ‌شات می‌شود تا ویرایش
    بعدیِ بسته، بدهیِ مشتری را جابه‌جا نکند. اعتبار فقط پس از تأیید سوپرادمین
    به سقف اضافه می‌شود — پس «خرید» خودش سرویس را باز نمی‌کند."""
    conn = get_conn(USER_RECORDS_DB)
    try:
        # بدون پلن، اعتبارِ خریده‌شده جایی برای نشستن ندارد (tenant_plans ردیف ندارد).
        # لینک فروشگاه پنهان است ولی مسیر مستقیم هست — اینجا جلویش گرفته می‌شود
        # تا مشتری برای خدمتی که قابل تحویل نیست پول ندهد.
        has_plan = conn.execute(
            "SELECT 1 FROM tenant_plans WHERE tenant_id=?", (tenant_id,)).fetchone()
        if not has_plan:
            raise ValueError("پلنی برای این مرکز تعریف نشده است — ابتدا با پشتیبانی تماس بگیرید.")
        pkg = conn.execute(
            "SELECT name, extra_messages, extra_introductions, price_rials, active "
            "FROM topup_packages WHERE id=?", (package_id,)).fetchone()
        if not pkg or not pkg[4]:
            raise ValueError("این بسته در دسترس نیست.")
        cur = conn.execute(
            "INSERT INTO topup_requests (tenant_id, package_id, package_name, "
            "extra_messages, extra_introductions, price_rials, requested_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (tenant_id, package_id, pkg[0], pkg[1], pkg[2], pkg[3], by or ""),
        )
        conn.commit()
        req_id = cur.lastrowid
    finally:
        conn.close()
    audit_service.log(by, "topup.request",
                      {"request_id": req_id, "tenant_id": tenant_id, "package_id": package_id})
    return req_id


def list_topup_requests(tenant_id: int | None = None, status: str = "pending") -> list[dict]:
    conn = get_conn(USER_RECORDS_DB)
    q = ("SELECT r.id, r.tenant_id, t.name, r.package_name, r.extra_messages, "
         "r.extra_introductions, r.price_rials, r.status, r.requested_by, r.requested_at "
         "FROM topup_requests r LEFT JOIN tenants t ON t.id = r.tenant_id ")
    args: list = []
    # شرطِ اول باید WHERE باشد نه AND — «AND» بعد از LEFT JOIN به ON می‌چسبد و
    # در LEFT JOIN فقط تعیین می‌کند ردیفِ سمتِ راست match شود یا نه، پس فیلترِ
    # tenant بی‌صدا حذف می‌شد و هر مشتری سوابقِ همه را می‌دید.
    where = []
    if status:
        where.append("r.status=?")
        args.append(status)
    if tenant_id is not None:
        where.append("r.tenant_id=?")
        args.append(tenant_id)
    if where:
        q += "WHERE " + " AND ".join(where) + " "
    q += "ORDER BY r.id DESC LIMIT 200"
    rows = conn.execute(q, args).fetchall()
    conn.close()
    return [
        {"id": r[0], "tenant_id": r[1], "tenant_name": r[2] or f"#{r[1]}",
         "package_name": r[3], "extra_messages": r[4], "extra_introductions": r[5],
         "price_rials": r[6], "status": r[7], "requested_by": r[8], "requested_at": r[9]}
        for r in rows
    ]


def resolve_topup(request_id: int, approve: bool, by: str = "") -> int:
    """تأیید/رد خرید. اعتبارِ خریده‌شده **پاک نمی‌شود** با ریست ماهانه —
    extra_* ستونِ جدا است و دست‌نخورده می‌ماند.

    خروجی: (tenant_id, extra_messages, extra_introductions)"""
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT tenant_id, extra_messages, extra_introductions, status "
            "FROM topup_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise ValueError("درخواست یافت نشد.")
        if row[3] != "pending":
            raise ValueError("این درخواست قبلاً بررسی شده است.")
        tenant_id, extra_msg, extra_intro = row[0], row[1], row[2]
        if approve:
            # مشتریِ بدون ردیفِ tenant_plans با این UPDATE ساکت ۰ سطر را می‌خورد
            # و اعتبارِ پولی گم می‌شود؛ باید قبلش صریح چک شود نه با rowcount بعدش.
            has_plan = conn.execute(
                "SELECT 1 FROM tenant_plans WHERE tenant_id=?", (tenant_id,)).fetchone()
            if not has_plan:
                raise ValueError("این مشتری پلنی ندارد — اعتبار قابل اعمال نیست.")
            conn.execute(
                "UPDATE tenant_plans SET extra_messages = extra_messages + ?, "
                "extra_introductions = extra_introductions + ? WHERE tenant_id=?",
                (extra_msg, extra_intro, tenant_id),
            )
        conn.execute(
            "UPDATE topup_requests SET status=?, resolved_by=?, resolved_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            ("approved" if approve else "rejected", by or "", request_id),
        )
        conn.commit()
    finally:
        conn.close()
    audit_service.log(by, "topup.resolve",
                      {"request_id": request_id, "approved": approve, "tenant_id": tenant_id})
    return tenant_id, extra_msg, extra_intro
