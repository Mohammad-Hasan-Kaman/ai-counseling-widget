# -*- coding: utf-8 -*-
"""
ماشین حالت گفتگوی پذیرش ویجت وب — موتور فلوی عمومی per-tenant
فلوی هر مشتری از tenants.flow_config خوانده می‌شود؛ فلوی tenant نمونه عین ربات بله است.
"""
import json
import logging
import re
import uuid
import html as html_mod
from datetime import datetime, timedelta, timezone

from app.config import (
    USER_RECORDS_DB, MAPPING_JSON, WIDGET_SESSION_TTL, MAX_MESSAGE_LENGTH, TEAM_PAGE_URL,
)
from app.db import get_conn
from app.internal_ai_engine import engine, record_learning_event, filter_profiles_for_user
from app.user_db import validate_phone_and_get_count, save_user_consultation, phone_error, normalize_phone
from app.ghq_analyzer import (
    GHQ_QUESTIONS, calculate_ghq_scores, get_ghq_message, GHQ_SEVERE_TOTAL,
)
from app import tenants as tenants_mod
from app import llm_flow
from app import plan_service

RESTART_WORDS = ("شروع مجدد", "شروع", "/start")
# دکمهٔ پیام قطعی — کلیکش سؤالِ جاری را دوباره می‌پرسد (سشن دست نمی‌خورد)
RETRY_WORDS = ("تلاش مجدد",)
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

logger = logging.getLogger(__name__)

RESTART_QR = ["شروع مجدد"]
RETRY_QR = ["تلاش مجدد"]
YES_NO_QR = ["بله", "خیر"]

GHQ_INTRO = (
    "📋 **آزمون غربالگری سلامت عمومی (GHQ-28)**\n\n"
    "تکمیل این آزمون اختیاری است اما به سیستم کمک می‌کند شرایط شما را با دقت بالینی بسیار بالاتری تحلیل کند.\n\n"
    "آیا مایل هستید این تست کوتاه (۲۸ سؤال) را پاسخ دهید؟"
)

GHQ_INTRO_FOR_OTHER = (
    "📋 **آزمون غربالگری سلامت عمومی (GHQ-28)**\n\n"
    "تکمیل این آزمون اختیاری است و کمک می‌کند شرایط مراجع را با دقت بالینی بسیار "
    "بالاتری تحلیل کنیم؛ پاسخ هر سؤال را از دیدِ خودِ مراجع بدهید.\n\n"
    "آیا مایل هستید این تست کوتاه (۲۸ سؤال) را پاسخ دهید؟"
)


def _ghq_intro(data: dict | None) -> str:
    if data and _is_for_other(data):
        return GHQ_INTRO_FOR_OTHER
    return GHQ_INTRO


class TenantDisabled(Exception):
    """سشن متعلق به مشتری غیرفعال/حذف‌شده است — لایه وب باید 403 بدهد."""


class _ChatOutage(Exception):
    """پلن متوسط: LLM در دسترس نبود و fallback مجاز نیست — گفتگو می‌ایستد."""

# کلیدهای استاندارد user_requests — بقیه در custom_data ذخیره می‌شوند
_SOURCE_KEYS = {"full_name", "phone", "gender", "age", "topic", "has_prev_therapy",
                "prev_detail", "expectation", "preferred_gender", "branch"}

# استپ «برای خودت یا دیگری؟» (Q17/Q23) — اولین مرحله وقتی فعال باشد
RELATIONSHIP_OPTIONS = [
    "برای خودم", "برای فرزندم", "برای والدینم", "برای همسرم",
    "برای خواهر یا برادرم", "برای دوستم", "برای سایر فامیل", "برای شخص دیگری",
]
RELATIONSHIP_STEP = {
    "key": "relationship", "type": "choice", "source": "relationship",
    "label": "این اطلاعات برای چه کسی وارد می‌شود؟ (خودتان یا شخص دیگری)",
    "options": list(RELATIONSHIP_OPTIONS),
}


def _is_for_other(data: dict) -> bool:
    rel = data.get("relationship") or ""
    return bool(rel) and rel != "برای خودم"


# روابطی که مراجعشان کودک/نوجوان است — سن و لحن باید مالِ «او» باشد نه ثبت‌کننده
_CHILD_RELATIONSHIPS = {"برای فرزندم", "برای والدینم"}


def _age_owner_phrase(data: dict) -> str:
    """خطابِ درست برای سن — «سن شما» یا «سنِ مراجع» (Q-برای دیگری).

    برای «برای فرزندم/والدینم»، سنِ کودک/سالمند پرسیده می‌شود نه سنِ ثبت‌کننده."""
    if (data.get("relationship") or "") in _CHILD_RELATIONSHIPS:
        return "سن ایشان"
    if _is_for_other(data):
        return "سن مراجع"
    return "سن شما"

_CHOICE_LABELS = {
    "بله": True, "خیر": False,
}


# ── تبدیل Markdown تلگرام به متن/HTML وب ──

def md_to_text(s: str) -> str:
    s = s.replace("**", "")
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", s)
    return s


def md_to_html(s: str) -> str:
    escaped = html_mod.escape(s)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)

    def _link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        # فقط http/https مجاز — جلوگیری از javascript: و schemeهای خطرناک
        if not re.match(r"https?://", url, re.IGNORECASE):
            return label
        # _top: داخل iframe ویجت، کل پنجره را می‌گیرد (تب جدید اگر مرورگر اجازه دهد،
        # وگرنه حداقل فریم والد را جایگزین می‌کند و صفحه سایت زیر ویجت دیده می‌شود)
        return f'<a href="{html_mod.escape(url, quote=True)}" target="_top" rel="noopener">{label}</a>'

    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, escaped)
    escaped = escaped.replace("\n", "<br>")
    return escaped


_profile_url_cache: dict = {}
_profile_url_cache_mtime: float = 0.0


def _profile_url(name: str) -> str:
    """لینک صفحه رزرو مشاور از mapping کراولر (با تطابق فازی نام و کش)."""
    global _profile_url_cache, _profile_url_cache_mtime
    try:
        mtime = MAPPING_JSON.stat().st_mtime if MAPPING_JSON.exists() else 0.0
    except Exception:
        mtime = 0.0
    mapping = _profile_url_cache.get("__map__")
    if mapping is None or mtime != _profile_url_cache_mtime:
        mapping = {}
        try:
            if MAPPING_JSON.exists():
                mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8")) or {}
        except Exception:
            mapping = {}
        _profile_url_cache = {"__map__": mapping}
        _profile_url_cache_mtime = mtime
    if name in mapping:
        return mapping[name]
    from app.internal_ai_engine import names_match as _names_match
    for k, v in mapping.items():
        try:
            if _names_match(name, k):
                return v
        except Exception:
            if name in k or k in name:
                return v
    return TEAM_PAGE_URL


# ── تشخیص گزینه GHQ (عیناً از main.py:120-131) ──

def _parse_ghq_choice(text: str, opts: list) -> int | None:
    clean_text = text.strip().translate(PERSIAN_DIGITS)
    m = re.search(r"^(\d+)", clean_text)
    if m and int(m.group(1)) in range(len(opts)):
        return int(m.group(1))
    # تطبیق متنی: ورودیِ بلندتر باید گزینهٔ کامل‌تر را بگیرد («بسیار بدتر از
    # معمول» نباید به‌خاطر زیررشته بودن «بدتر از معمول» گزینهٔ ملایم‌تر شود)
    # و ورودیِ کوتاه باید کوتاه‌ترین گزینهٔ حاوی آن باشد («کمتر» → نه «بسیار کمتر»)
    best_in_text = None   # گزینه‌ای که داخل ورودی جا می‌شود → بلندترین
    best_contains = None  # ورودی داخل گزینه → کوتاه‌ترین
    for i, opt in enumerate(opts):
        if opt and opt in clean_text:
            if best_in_text is None or len(opt) > len(opts[best_in_text]):
                best_in_text = i
        elif clean_text and clean_text in opt:
            if best_contains is None or len(opt) < len(opts[best_contains]):
                best_contains = i
    if best_in_text is not None:
        return best_in_text
    if best_contains is not None:
        return best_contains
    m_any = re.search(r"(\d+)", clean_text)
    if m_any and int(m_any.group(1)) in range(len(opts)):
        return int(m_any.group(1))
    return None


# ── ساختار پاسخ ربات ──

class BotReply:
    def __init__(self, text: str, quick_replies: list[str] | None = None,
                 input_type: str = "text", done: bool = False):
        self.text = text
        self.quick_replies = quick_replies or []
        self.input_type = input_type
        self.done = done

    @property
    def html(self) -> str:
        return md_to_html(self.text)


def _outage_reply() -> BotReply:
    """پیام قطعی با دکمهٔ «تلاش مجدد» — لاگ در ترنسکریپت بر عهدهٔ فراخواننده.

    متن OUTAGE_MSG خودش می‌گوید «کمی بعد دوباره تلاش کنید»؛ دکمه همان تلاش
    را همین حالا ممکن می‌کند (سؤالِ جاری را دوباره می‌پرسد). ورودی متن هم
    می‌ماند چون سشن دست‌نخورده است — کاربر می‌تواند همان پاسخ قبلی را دوباره بفرستد."""
    return BotReply(llm_flow.OUTAGE_MSG, quick_replies=RETRY_QR, input_type="chips+text")


def _ghq_quick_replies(idx: int) -> list[str]:
    _, opts = GHQ_QUESTIONS[idx]
    return [f"{i} - {o}" for i, o in enumerate(opts)]


def _ghq_question_text(idx: int) -> str:
    q, _ = GHQ_QUESTIONS[idx]
    return (
        f"📝 **سؤال {idx+1} از ۲۸:**\n\n"
        f"{q}\n\n"
        f"👇 *لطفاً یکی از گزینه‌های زیر را انتخاب فرمایید:*"
    )


# ── سشن ──

def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=WIDGET_SESSION_TTL)).strftime("%Y-%m-%d %H:%M:%S")


def create_session(tenant_id: int) -> str:
    token = uuid.uuid4().hex
    conn = get_conn(USER_RECORDS_DB)
    conn.execute(
        "INSERT INTO widget_sessions (token, state, data, expires_at, tenant_id) VALUES (?, ?, ?, ?, ?)",
        (token, 0, json.dumps({}), _expires(), tenant_id),
    )
    conn.commit()
    conn.close()
    return token


def get_session(token: str) -> dict | None:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT state, data, tenant_id FROM widget_sessions WHERE token=? AND expires_at >= datetime('now')",
        (token,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"token": token, "state": row[0], "data": json.loads(row[1]), "tenant_id": row[2]}


def _save_session(token: str, state: int, data: dict):
    conn = get_conn(USER_RECORDS_DB)
    conn.execute(
        "UPDATE widget_sessions SET state=?, data=?, updated_at=CURRENT_TIMESTAMP, expires_at=? WHERE token=?",
        (state, json.dumps(data, ensure_ascii=False), _expires(), token),
    )
    conn.commit()
    conn.close()


def log_message(token: str, role: str, text: str):
    conn = get_conn(USER_RECORDS_DB)
    try:
        try:
            conn.execute(
                "INSERT INTO widget_messages (token, role, text, tenant_id) VALUES (?, ?, ?, (SELECT tenant_id FROM widget_sessions WHERE token=?))",
                (token, role, text, token),
            )
        except Exception:
            # دیتابیس قدیمی بدون ستون tenant_id
            conn.execute(
                "INSERT INTO widget_messages (token, role, text) VALUES (?, ?, ?)",
                (token, role, text),
            )
        conn.commit()
    finally:
        conn.close()


# ── کمک‌های فلوی عمومی ──

_STEP_DEFAULT_LABELS = {
    "full_name": "لطفاً نام و نام خانوادگی خود را وارد فرمایید:",
    "phone": "لطفاً شماره تماس خود را وارد کنید (مانند 09123456700):",
    "gender": "جنسیت شما:",
    "age": "سن شما (به عدد):",
    "topic": "لطفاً موضوع اصلی یا مشکلی که برای آن به مشاوره نیاز دارید را بنویسید:",
    "prev_therapy": "آیا سابقه مراجعه قبلی به روان‌شناس یا روان‌پزشک داشته‌اید؟",
    "prev_detail": "در صورت تمایل توضیح مختصری درباره درمان قبلی بنویسید (در غیر این صورت یک پیام کوتاه ارسال کنید):",
    "expectation": "انتظار یا رویکرد مدنظر شما از جلسات مشاوره چیست؟",
    "preferred_gender": "ترجیح می‌دهید جنسیت مشاور شما چه باشد؟",
    "branch": "کدام شعبه مرکز مشاوره نمونه برای شما مناسب‌تر است؟",
}

# وقتی فرم برای دیگری پر می‌شود: کدام سؤال‌ها به خودِ ثبت‌کننده مربوط‌اند
# (relationship که از اولِ گفتگو از خودِ کاربر پرسیده می‌شود؛ phone برای
# هماهنگیِ نوبت با کسی که فرم را پر کرده؛ spouse_relation سؤالِ والد از
# والدِ ثبت‌کننده است). بقیهٔ سؤال‌ها — از جمله ترجیح جنسیت مشاور و شعبه —
# مالِ خودِ مراجع است.
_INITIATOR_STEPS = {"relationship", "phone", "spouse_relation"}

_INITIATOR_ASK_TEXT = (
    "حالا لطفاً **نام و نام خانوادگی خودتان** (شخصی که فرم را پر می‌کند) را وارد کنید:"
)


def _ask_brief(label: str, key: str, data: dict | None) -> str:
    """بریفِ سؤال برای AI: برچسب + راهنمای مخاطب.

    راهنما فقط برای فهمِ AI است (کجا در متن نیاید — در پرامپت سیستم گفته شده)؛
    برای همین کوتاه و اسمی است تا الگوی جملهٔ قابل‌کپی نداشته باشد."""
    if key == "relationship":
        return label
    who = "مخاطب: خودِ مخاطبِ همین گفتگو"
    if data and _is_for_other(data):
        who = ("مخاطب: ثبت‌کننده" if key in _INITIATOR_STEPS
               else "مخاطب: مراجع، نه ثبت‌کننده")
    return f"{label} | {who}"


def _session_history(token: str, limit: int = 8) -> list[dict]:
    """آخرین پیام‌های همین سشن برای context LLM (Q27) — نقش bot → assistant"""
    try:
        conn = get_conn(USER_RECORDS_DB)
        rows = conn.execute(
            "SELECT role, text FROM widget_messages WHERE token=? ORDER BY id DESC LIMIT ?",
            (token, limit),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    out = []
    for role, text in reversed(rows):
        out.append({"role": "assistant" if role == "bot" else "user", "content": (text or "")[:500]})
    return out


def _tone(llm_cfg: dict | None, text: str, history: list | None = None) -> str:
    """لحن‌بخشی LLM فقط در پلن‌های LLM‌دار؛ خطا → متن خام (Q22)"""
    return llm_flow.rewrite_tone(llm_cfg, text, history=history)


def _step_prompt(flow: dict, step_idx: int, llm_cfg: dict | None = None,
                 data: dict | None = None, history: list | None = None) -> BotReply:
    """پرسش مرحله step_idx از فلوی مشتری.

    در پلن‌های LLM‌دار، LLM همین پرسش را انسانی بازگو می‌کند (chat_ask) تا ربات
    فرم پر نکند؛ ماشین حالت و ترتیب مراحل دست‌نخورده می‌ماند. اگر LLM خطا داد
    یا جوابش خالی بود، به متنِ قاعده‌ای برمی‌گردیم (پیشرفته) — متوسط از
    فراخوانِ جداگانهٔ خودش مراقبت می‌کند."""
    step = flow["steps"][step_idx]
    label = step.get("label") or _STEP_DEFAULT_LABELS.get(step["key"], f"لطفاً {step['key']} را وارد کنید:")
    # تلفن: اگر برای دیگری ثبت می‌شود، شماره مال ثبت‌کننده است (Q17)
    if step.get("key") == "phone" and data and _is_for_other(data):
        label = "شماره تماس شما (ثبت‌کننده/تماس‌گیرنده) — برای هماهنگی کارشناسان (مانند 09123456700):"
    # سن: برای «برای فرزندم» سنِ کودک پرسیده می‌شود، نه سنِ ثبت‌کننده
    if step.get("key") == "age" and data:
        who = _age_owner_phrase(data)
        if who != "سن شما":
            label = f"{who} چند سال است؟ (فقط عدد بنویسید)"
    # بقیهٔ سؤال‌های مربوط به مراجع هم وقتی فرم برای دیگری است «شما» نمی‌گویند
    if data and _is_for_other(data):
        _other_labels = {
            "full_name": "نام و نام خانوادگی مراجع را وارد کنید:",
            "gender": "جنسیت مراجع (خانم یا آقا):",
            "topic": "موضوع اصلی مشکلِ مراجع چیست؟",
            "prev_therapy": "آیا مراجع سابقه مراجعه قبلی به روان‌شناس یا روان‌پزشک داشته است؟",
            "prev_detail": "در صورت تمایل توضیح مختصری دربارهٔ سابقه درمان قبلیِ مراجع بنویسید (در غیر این صورت یک پیام کوتاه ارسال کنید):",
            "preferred_gender": "ترجیح مراجع در جنسیت مشاور چیست؟",
            "branch": "کدام شعبه برای مراجع مناسب‌تر است؟",
        }
        label = _other_labels.get(step.get("key"), label) or label
    stype = step["type"]
    quick = list(step.get("options") or ["بله", "خیر"]) if stype == "choice" else []

    # گفتگوی انسانی: پرسش را AI بازگو می‌کند تا فرم خشک نباشد — برای همهٔ
    # استپ‌ها (انتخابی‌ها هم؛ چیپس‌ها جداگانه حفظ می‌شوند).
    if llm_cfg:
        natural, outage = llm_flow.chat_ask(
            llm_cfg, _ask_brief(label, step.get("key", ""), data),
            data or {}, (flow.get("_tenant_name") or ""), history,
            has_options=(stype == "choice"),
        )
        if natural:
            return BotReply(
                natural, quick_replies=quick,
                input_type="chips" if stype == "choice" else "text",
            )
        if outage and llm_cfg.get("mode") == plan_service.MODE_MEDIUM:
            # متوسط: بدون fallback — گفتگو می‌ایستد (Q22)
            raise _ChatOutage()

    label = _tone(llm_cfg, label, history)
    return BotReply(label, quick_replies=quick, input_type="chips" if stype == "choice" else "text")


_LEGACY_COND_ALIASES = {"prev_therapy": "has_prev_therapy", "has_prev_therapy": "prev_therapy"}


def _conditional_satisfied(data: dict, cond: str | None) -> bool:
    """مرحله شرطی (مثل prev_detail فقط اگر prev_therapy=بله) رد شود یا نه"""
    if not cond:
        return True
    key, _, val = cond.partition("=")
    key, val = key.strip(), val.strip()
    actual = data.get(key)
    # سازگاری شرط‌های قدیمی (نام کلید عوض شده)
    if actual is None and key in _LEGACY_COND_ALIASES:
        actual = data.get(_LEGACY_COND_ALIASES[key])
    # مقدار ذخیره‌شده ممکن است بولین باشد (بله/خیر → True/False)
    if isinstance(actual, bool):
        actual = "بله" if actual else "خیر"
    return str(actual) == val


def _advance_state(session: dict, flow: dict, current: int):
    """به مرحله بعدی غیرشرطی می‌رود؛ خروجی: شماره مرحله بعدی یا 'ask_ghq' یا 'ghq' یا 'end'"""
    steps = flow["steps"]
    idx = current + 1
    while idx < len(steps):
        step = steps[idx]
        # رد کردن مراحل شرطی که شرطشان برقرار نیست
        if not _conditional_satisfied(session["data"], step.get("conditional")):
            idx += 1
            continue
        return idx
    if flow.get("ghq"):
        return "ask_ghq"
    return "end"


def opening_reply(flow: dict, llm_cfg: dict | None = None,
                  history: list | None = None) -> BotReply:
    """خوش‌آمد + اولین سؤالِ فلو در یک پاسخ.

    welcome می‌گوید «به چند سؤال کوتاه زیر پاسخ دهید» ولی تا پیش از این خودِ
    سؤالِ اول فقط بعد از نخستین پیامِ کاربر پرسیده می‌شد — کاربر متنی را می‌دید
    که به سؤالی اشاره می‌کرد و آن سؤال اصلاً صفحه نبود.

    llm_cfg فقط جایی پاس می‌شود که مشارکت AI شمرده می‌شود (handle_message)؛
    باز کردنِ سشن metering ندارد و نباید لحنِ رایگان بگیرد."""
    welcome = _tone(llm_cfg, flow.get("welcome") or "سلام! خوش آمدید.", history)
    if not flow.get("steps"):
        return BotReply(welcome)
    try:
        first = _step_prompt(flow, 0, llm_cfg, None, history)
    except _ChatOutage:
        # خوش‌آمد هرگز بدون سؤال اول نماند — قطعی LLM هم باید سؤال اول
        # را با همان متنِ قاعده‌ای بپرسد (تلاش مجددِ LLM با پیام بعدی)
        first = _step_prompt(flow, 0, None, None, None)
    return BotReply(
        welcome + "\n\n" + first.text,
        quick_replies=first.quick_replies,
        input_type=first.input_type,
    )


def _restart(session: dict, flow: dict, llm_cfg: dict | None = None,
             history: list | None = None) -> BotReply:
    reply = opening_reply(flow, llm_cfg, history)
    _save_session(session["token"], 0, {})
    log_message(session["token"], "bot", md_to_text(reply.text))
    return reply


def _ask_initiator(flow: dict, llm_cfg: dict | None, data: dict,
                   history: list | None) -> BotReply:
    """نام خودِ ثبت‌کننده را بپرس (Q17). سشن (رابطه و state) از قبل ذخیره شده؛
    متوسط روی قطعی LLM بالا می‌اندازد و پیام بعدی همین پرسش را دوباره می‌آورد."""
    init_brief = "نام و نام خانوادگی ثبت‌کننده | مخاطب: خودِ ثبت‌کننده"
    init_text = _INITIATOR_ASK_TEXT
    if llm_cfg:
        natural, outage = llm_flow.chat_ask(
            llm_cfg, init_brief, data, (flow.get("_tenant_name") or ""), history,
        )
        if natural:
            init_text = natural
        elif outage and llm_cfg.get("mode") == plan_service.MODE_MEDIUM:
            raise _ChatOutage()
        else:
            init_text = _tone(llm_cfg, init_text, history)
    return BotReply(init_text, quick_replies=[])


def _reask_current(session: dict, flow: dict, llm_cfg: dict | None,
                   history: list | None) -> BotReply:
    """دکمهٔ «تلاش مجدد»: سؤالِ جاری را دوباره می‌پرسد — هیچ پاسخی مصرف یا
    حذف نمی‌شود و state جلو نمی‌رود. اگر LLM هنوز قطع باشد، همان پیام قطعی
    با دکمه برمی‌گردد تا کاربر دوباره بزند."""
    token = session["token"]
    data = session["data"]
    state = session["state"]

    # مچینگِ پایانی قطع شده — دوباره تلاش برای معرفی (لید قبلاً ذخیره شده)
    if state == -2 and data.get("_retry_match"):
        return _retry_match(session, flow, llm_cfg, history)

    if state == 0:
        reply = opening_reply(flow, llm_cfg, history)  # خوش‌آمد + سؤال اول (همیشه)
        log_message(token, "bot", md_to_text(reply.text))
        return reply

    if data.get("_need_initiator") and state >= 0:
        ask = _ask_initiator(flow, llm_cfg, data, history)  # ممکن است _ChatOutage بدهد
        log_message(token, "bot", md_to_text(ask.text))
        return ask

    if 0 <= state < len(flow["steps"]):
        prompt = _step_prompt(flow, state, llm_cfg, data, history)  # ممکن است _ChatOutage بدهد
        log_message(token, "bot", md_to_text(prompt.text))
        return prompt

    reply = _state_prompt(session)  # GHQ / پایان — بدون LLM، هرگز raise نمی‌کند
    log_message(token, "bot", md_to_text(reply.text))
    return reply


def _retry_match(session: dict, flow: dict, llm_cfg: dict | None,
                 history: list | None) -> BotReply:
    """تلاش مجددِ مچینگِ قطع‌شده — لید در نوبت قطعی ثبت شده، دوباره ذخیره نمی‌شود
    تا شمارندهٔ مراجعات و ردیف درخواست دوبل نشود."""
    reply = _recommend(session, flow, llm_cfg, history, lead_saved=True)
    if "](" not in reply.text:
        reply.text = _tone(llm_cfg, reply.text, history)
    # فلگ _retry_match ممکن است در همین فراخوانی پاک (موفق) یا دوباره ست (قطعی) شده باشد
    _save_session(session["token"], -2, session["data"])
    log_message(session["token"], "bot", md_to_text(reply.text))
    return BotReply(
        reply.text,
        quick_replies=reply.quick_replies or RESTART_QR,
        input_type="chips", done=True,
    )


# ── پردازش پاسخ هر مرحله ──

def _handle_step(session: dict, flow: dict, text: str, llm_cfg: dict | None = None,
                 history: list | None = None) -> BotReply:
    data = session["data"]
    step = flow["steps"][session["state"]]
    stype = step["type"]

    # گزینه‌های مراحل انتخابی باید همیشه موجود باشند
    if stype == "choice" and not step.get("options"):
        step["options"] = ["بله", "خیر"]

    value = None

    if stype == "text":
        if len(text) < 1:
            return _step_prompt(flow, session["state"], llm_cfg)
        value = text

    elif stype == "phone":
        # تلفن هرگز به LLM نمی‌رود (Q27) — اعتبارسنجی قاعده‌ای سخت‌گیرانه
        phone = normalize_phone(text)
        err = phone_error(phone)
        if err:
            _invalid = BotReply(err)
            log_message(session["token"], "bot", md_to_text(_invalid.text))
            return _invalid
        full_name = data.get("full_name", "")
        # هیچ‌کس بلاک نمی‌شود؛ تعارض‌ها در پنل ادمین بررسی می‌شوند
        _, req_count, _ = validate_phone_and_get_count(phone, full_name, session["tenant_id"])
        data["phone"] = phone
        data["request_count"] = req_count
        value = phone

    elif stype == "number":
        t = text.translate(PERSIAN_DIGITS)
        if t.isdigit() and (0 < int(t) < 150):
            value = int(t)
        elif llm_cfg:
            # فهم متن آزاد با LLM («بیست و سه سال» → 23)
            parsed, outage = llm_flow.interpret_number(llm_cfg, text, history=history)
            if outage and llm_cfg.get("mode") == plan_service.MODE_MEDIUM:
                # متوسط بدون fallback: پیام خطا، بدون پیشرفت (Q22)
                log_message(session["token"], "bot", llm_flow.OUTAGE_MSG)
                return _outage_reply()
            if parsed is not None and 0 < parsed < 150:
                value = parsed
            elif outage:
                log_message(session["token"], "bot", llm_flow.OUTAGE_MSG)
                return _outage_reply()  # advanced: خطای LLM ولی اینجا قاعده هم نبود
            else:
                return BotReply(_tone(llm_cfg, "لطفاً یک عدد معتبر وارد فرمایید:", history))
        else:
            return BotReply("لطفاً یک عدد معتبر وارد فرمایید:")

    elif stype == "choice":
        opts = list(step.get("options") or ["بله", "خیر"])
        if text in opts:
            value = text
        elif llm_cfg:
            mapped, outage = llm_flow.interpret_choice(llm_cfg, text, opts, history=history)
            if mapped is not None:
                value = mapped
            elif outage:
                # خطا LLM: متوسط = پیام خطا؛ advanced = رفتار قاعده‌ای (re-ask)
                if llm_cfg.get("mode") == plan_service.MODE_MEDIUM:
                    log_message(session["token"], "bot", llm_flow.OUTAGE_MSG)
                    return _outage_reply()
                return BotReply(
                    _tone(llm_cfg, "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:", history),
                    quick_replies=opts, input_type="chips",
                )
            else:
                return BotReply(
                    _tone(llm_cfg, "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:", history),
                    quick_replies=opts, input_type="chips",
                )
        else:
            return BotReply("لطفاً یکی از گزینه‌های زیر را انتخاب کنید:", quick_replies=opts, input_type="chips")

    # ذخیره مقدار: کلید استاندارد یا custom
    key = step["key"]
    # نگاشت شرطی قدیمی (has_prev_therapy=بله) روی کلید فعلی prev_therapy —
    # بولین ذخیره شود (همان قرارداد _CHOICE_LABELS): رشتهٔ «خیر» در ذخیرهٔ
    # نهایی truthy است و سابقهٔ نداشته را «بله» ثبت می‌کرد
    cond = step.get("conditional") or ""
    if cond.partition("=")[0].strip() == "has_prev_therapy" and data.get("prev_therapy") in ("بله", "خیر"):
        data["has_prev_therapy"] = data["prev_therapy"] == "بله"
    if key == "prev_therapy" and value in ("بله", "خیر"):
        data["has_prev_therapy"] = value == "بله"
    src = step.get("source", key)
    if src in _SOURCE_KEYS:
        if src == "has_prev_therapy":
            data[src] = _CHOICE_LABELS.get(value, value)
        else:
            data[src] = value
        custom = data.get("_custom") or {}
        if custom:
            data["_custom"] = custom
    else:
        custom = data.setdefault("_custom", {})
        custom[key] = value
    data[key] = value

    # برای دیگری: پیام بعدی، نام خودِ ثبت‌کننده است (Q17 الف)
    if key == "relationship" or src == "relationship":
        if value != "برای خودم":
            data["_need_initiator"] = 1

    nxt = _advance_state(session, flow, session["state"])

    if nxt == "end":
        return _finish(session, flow, llm_cfg=llm_cfg, history=history)
    if nxt == "ask_ghq":
        # سؤال قبولی GHQ — عین ربات (اختیاری بودن آزمون) — بدون LLM (Q27)
        _save_session(session["token"], -3, data)  # -3 = پرسش قبولی GHQ
        intro = _ghq_intro(data)
        log_message(session["token"], "bot", md_to_text(intro))
        return BotReply(intro, quick_replies=YES_NO_QR, input_type="chips")
    if nxt == "ghq":
        data["ghq_answers"] = []
        data["ghq_index"] = 0
        _save_session(session["token"], -1, data)  # -1 = GHQ جاری
        qtext = _ghq_question_text(0)
        log_message(session["token"], "bot", md_to_text(qtext))
        return BotReply(qtext, quick_replies=_ghq_quick_replies(0), input_type="chips+text")

    # برای دیگری: اول نام خودِ ثبت‌کننده — بدون تولید/لاگِ بیهودهٔ سؤال بعدی
    if data.get("_need_initiator"):
        _save_session(session["token"], nxt, data)
        ask = _ask_initiator(flow, llm_cfg, data, history)
        log_message(session["token"], "bot", md_to_text(ask.text))
        return ask

    # پرسش مرحلهٔ بعدی اول ساخته می‌شود (ممکن است _ChatOutage بدهد) و بعد ذخیره —
    # تا outage واقعاً سشن را دست‌نخورده بگذارد، همچون مسیرهای interpret_choice/number.
    prompt = _step_prompt(flow, nxt, llm_cfg, data, history)
    _save_session(session["token"], nxt, data)
    log_message(session["token"], "bot", prompt.text)

    # پیام خوش‌آمد مراجع مکرر — فقط بعد از ثبت شماره
    if stype == "phone" and data.get("request_count", 0) > 1:
        full_name = data.get("full_name", "")
        tenant_name = flow.get("_tenant_name") or "مرکز"
        welcome = f"🌹 خوش‌آمدید {full_name} عزیز! این **بار {data['request_count']}‌ام** است که در {tenant_name} در خدمت شما هستیم."
        toned = _tone(llm_cfg, welcome, history).rstrip()
        return BotReply(toned + "\n\n" + prompt.text, quick_replies=prompt.quick_replies, input_type=prompt.input_type)

    return prompt


def _handle_ghq(session: dict, flow: dict, text: str, llm_cfg: dict | None = None) -> BotReply:
    data = session["data"]
    idx = data.get("ghq_index", 0)
    if idx >= len(GHQ_QUESTIONS):
        return _finish(session, flow, llm_cfg=llm_cfg)
    _, opts = GHQ_QUESTIONS[idx]
    choice = _parse_ghq_choice(text, opts)
    if choice is None:
        return BotReply(
            "⚠️ لطفاً تنها یکی از گزینه‌های زیر را انتخاب کنید:",
            quick_replies=_ghq_quick_replies(idx), input_type="chips+text",
        )
    data.setdefault("ghq_answers", []).append(choice)
    data["ghq_index"] = idx + 1

    if data["ghq_index"] >= len(GHQ_QUESTIONS):
        scores = calculate_ghq_scores(data["ghq_answers"])
        data["ghq_scores"] = scores
        ghq_msg = get_ghq_message(scores)
        # llm_cfg رد می‌شود: متن GHQ لحن نمی‌خورد (Q27)، ولی مچینگ پایانی از پلن پیروی می‌کند
        return _finish(session, flow, ghq_prepend=ghq_msg, llm_cfg=llm_cfg)

    _save_session(session["token"], -1, data)
    qtext = _ghq_question_text(data["ghq_index"])
    log_message(session["token"], "bot", md_to_text(qtext))
    return BotReply(qtext, quick_replies=_ghq_quick_replies(data["ghq_index"]), input_type="chips+text")


# ── پایان فلو ──

def _finish(session: dict, flow: dict, ghq_prepend: str | None = None,
            llm_cfg: dict | None = None, history: list | None = None) -> BotReply:
    data = session["data"]
    custom = data.get("_custom")
    ending = flow.get("ending", "lead_capture")

    if ending == "recommend":
        reply = _recommend(session, flow, llm_cfg, history)
    else:
        reply = _lead_capture(session, flow, llm_cfg)

    if ghq_prepend:
        reply.text = ghq_prepend + "\n\n" + reply.text
    elif "](" not in reply.text:
        # لحن پایانی فقط بدون لینک رزرو و بدون پیش‌درآمد GHQ (Q27)
        reply.text = _tone(llm_cfg, reply.text, history)

    data.pop("_custom", None)
    _save_session(session["token"], -2, data)  # -2 = DONE
    log_message(session["token"], "bot", md_to_text(reply.text))
    # قطعی مچینگ دکمهٔ «تلاش مجدد» می‌آورد؛ بقیهٔ پایان‌ها «شروع مجدد»
    return BotReply(reply.text, quick_replies=reply.quick_replies or RESTART_QR,
                    input_type="chips", done=True)


def _build_user_info(data: dict) -> dict:
    prev = data.get("has_prev_therapy", data.get("prev_therapy", False))
    initiator = data.get("initiator_name") or data.get("full_name", "")
    return {
        "full_name": data.get("full_name", ""),
        "phone": data.get("phone", ""),
        "gender": data.get("gender", ""),
        "age": data.get("age", 0),
        "topic": data.get("topic", ""),
        "has_prev_therapy": prev,
        "prev_detail": data.get("prev_detail", ""),
        "expectation": data.get("expectation", ""),
        "preferred_gender": data.get("preferred_gender", data.get("preferred_gender_1", "فرقی ندارد")),
        "branch": data.get("branch", data.get("branch_1", "اهمیتی ندارد")),
        "location": data.get("branch", data.get("branch_1", "اهمیتی ندارد")),
        "ghq_scores": data.get("ghq_scores", None),
        # «برای دیگری» (Q17): مراجع = full_name، تماس‌گیرنده = initiator
        "relationship": data.get("relationship", ""),
        "initiator_name": initiator,
        "is_for_other": _is_for_other(data),
    }


def _merged_custom(data: dict) -> dict:
    """custom_data + رابطه/ثبت‌کننده برای ذخیره در user_requests"""
    custom = dict(data.get("_custom") or {})
    if data.get("relationship"):
        custom["relationship"] = data["relationship"]
    if data.get("initiator_name"):
        custom["initiator_name"] = data["initiator_name"]
    elif data.get("full_name"):
        custom["initiator_name"] = data["full_name"]
    return custom


def _couple_advice(data: dict) -> str | None:
    """والدی که کیفیت رابطه با همسرش را میانه/پایین ارزیابی کرده — در پایان
    پیشنهاد زوج‌درمانگر بیاید (خواستهٔ محصول روی سؤال شرطی spouse_relation)."""
    if data.get("spouse_relation") in ("میانه", "پایین"):
        return "💡 پیشنهاد می‌شود به زوج درمانگر نیز مراجعه فرمایید."
    return None


def _recommend(session: dict, flow: dict, llm_cfg: dict | None = None,
               history: list | None = None, lead_saved: bool = False) -> BotReply:
    data = session["data"]
    tenant_id = session["tenant_id"]
    user_info = _build_user_info(data)

    recs = None
    if llm_cfg:
        # LLM‌محور (medium/advanced) با grounding روی نام‌های واقعی (Q10/Q12)
        profiles = engine.get_profiles(tenant_id)
        # فیلترهای صلب (جنسیت/شعبه/سن/حقوقی) در مسیر LLM هم — وگرنه پلن
        # پولی از weak سست‌تر می‌شد: فقط نام grounding می‌شد، نه شرایط
        profiles = filter_profiles_for_user(user_info, profiles)
        llm_recs, outage = llm_flow.llm_match(llm_cfg, user_info, profiles, top_n=2,
                                              tenant_id=tenant_id, history=history)
        if outage:
            if llm_cfg.get("mode") == plan_service.MODE_MEDIUM:
                # متوسط: بدون fallback — لید ذخیره می‌شود ولی معرفی نداریم (Q22).
                # فلگ، «تلاش مجدد» را برای همین مچینگ فعال می‌کند (lead_saved:
                # تلاش دوباره لید/شمارنده را دوبل نمی‌کند)
                if not lead_saved:
                    save_user_consultation(session["token"], user_info, [], tenant_id, _merged_custom(data))
                data["_retry_match"] = 1
                return _outage_reply()
            # advanced: fallback به موتور قاعده‌ای
            recs = engine.match(user_info, tenant_id)
        else:
            recs = llm_recs or []
            # تبدیل به ساختار engine برای ذخیره/یادگیری (score از LLM نمی‌آید)
            recs = [{"name": r["name"], "reason": r["reason"], "score": 0,
                     "free_slots": 0, "gender": "", "location": ""} for r in recs]
    else:
        # weak یا بدون LLM: موتور قاعده‌ای فعلی
        recs = engine.match(user_info, tenant_id)

    # از اینجا به بعد معرفی (یا نبودِ کاندید) قطعی است — تلاش قبلی دیگر لازم نیست
    data.pop("_retry_match", None)

    if not recs:
        # لید باید ثبت شود: مشتری تازه هنوز پروفایل مشاوری ندارد و قبلاً
        # همهٔ مراجعانش بی‌سروصدا گم می‌شدند (بدون معرفی = بدون سوزاندن سقف هم)
        if not lead_saved:
            save_user_consultation(session["token"], user_info, [], tenant_id, _merged_custom(data))
        apology = (
            "متأسفانه در حال حاضر مشاوری با این مشخصات یافت نشد؛ "
            "اما اطلاعات شما ثبت شد و کارشناسان مرکز آن را بررسی می‌کنند."
        )
        advice = _couple_advice(data)
        if advice:
            apology += "\n\n" + advice
        return BotReply(apology)

    # مصرف اتمیک سقف قبل از نمایش — اگر سقف همزمان تمام شده باشد، معرفی نکن (Q13/Q19)
    if not plan_service.increment_introductions(tenant_id):
        if not lead_saved:
            save_user_consultation(session["token"], user_info, [], tenant_id, _merged_custom(data))
        return BotReply(plan_service.lock_message_for(tenant_id))

    # تلاش مجدد بعد از قطعی: لید قبلاً ثبت شده — دوباره ثبت نکن تا
    # request_count و ردیف user_requests دوبل نشود
    req_number = 0 if lead_saved else save_user_consultation(
        session["token"], user_info, recs, tenant_id, _merged_custom(data)
    )

    ghq = user_info["ghq_scores"]
    ghq_lvl = "severe" if (ghq and ghq.get("total", 0) >= GHQ_SEVERE_TOTAL) else "normal"
    if plan_service.tenant_has_mode(tenant_id, plan_service.MODE_ADVANCED):
        for r in recs:
            record_learning_event(r["name"], user_info["topic"], ghq_level=ghq_lvl, success=True, tenant_id=tenant_id)

    count_badge = f" (ثبت درخواست نوبت {req_number} شما)" if req_number > 1 else ""
    tenant_name = flow.get("_tenant_name") or "مرکز"
    # مشاور برای خودِ مراجع انتخاب می‌شود — نه برای ثبت‌کننده
    if _is_for_other(data):
        who = data.get("full_name") or "مراجع"
    else:
        who = "شما"
    msg = f"🎯 **مشاوران برگزیده {tenant_name} برای {who}{count_badge}:**\n\n"
    for i, r in enumerate(recs, 1):
        msg += f"🏅 **گزینه {i}: {r['name']}**\n"
        msg += f"📋 دلیل پیشنهاد: {r['reason']}\n"
        # «نوبت آزاد» از متنِ دلیل بیرون است تا جای متمایزکنندهٔ دو گزینه را نگیرد
        if r.get("free_slots"):
            msg += f"🗓 نوبت آزاد: {r['free_slots']} نوبت\n"
        url = _profile_url(r["name"]) if flow.get("_tenant_slug", "demo") == "demo" else None
        if url:
            msg += f"🔗 [مشاهده نوبت‌ها و رزرو در سایت]({url})\n"
        msg += "\n"

    advice = _couple_advice(data)
    if advice:
        msg += advice + "\n\n"
    msg += "✨ برای ارزیابی مجدد یا ثبت درخواست جدید، گزینه «شروع مجدد» را انتخاب کنید."
    return BotReply(msg)


def _lead_capture(session: dict, flow: dict, llm_cfg: dict | None = None) -> BotReply:
    data = session["data"]
    tenant_id = session["tenant_id"]
    user_info = _build_user_info(data)
    save_user_consultation(session["token"], user_info, [], tenant_id, _merged_custom(data))
    thanks = flow.get("thanks_text", "متشکریم! اطلاعات شما ثبت شد.")
    advice = _couple_advice(data)
    if advice:
        thanks += "\n\n" + advice
    return BotReply(thanks)


# ── بازیابی سشن (رفرش صفحه) ──

def _state_prompt(session: dict) -> BotReply:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT flow_config FROM tenants WHERE id=?", (session["tenant_id"],)).fetchone()
    conn.close()
    flow = tenants_mod._parse_flow_config(row[0] if row else None)

    state = session["state"]
    if state == 0:
        return opening_reply(flow)
    if state == -1:
        data = session["data"]
        if data.get("ghq_index") is not None and data.get("ghq_answers") is not None:
            idx = data["ghq_index"]
            if idx < len(GHQ_QUESTIONS):
                return BotReply(_ghq_question_text(idx), quick_replies=_ghq_quick_replies(idx), input_type="chips+text")
            # همه پاسخ‌ها داده شده ولی finish ذخیره نشده (سشن قدیمی) — همان خلاصه را برگردان
            scores = calculate_ghq_scores(data["ghq_answers"])
            return BotReply(get_ghq_message(scores), quick_replies=RESTART_QR, input_type="chips", done=True)
        # GHQ شروع نشده — به مرحله جاری برگرد
        return _restart(session, flow)
    if state == -2:
        # مچینگِ قطع‌شده هنوز در انتظار «تلاش مجدد» است — پیام پایانیِ ثابت
        # دکمه را پنهان می‌کرد و کاربر را به «شروع مجدد» گمراه می‌کرد
        if session["data"].get("_retry_match"):
            return _outage_reply()
        return BotReply(
            "✨ برای ارزیابی مجدد یا ثبت درخواست جدید، گزینه «شروع مجدد» را انتخاب کنید.",
            quick_replies=RESTART_QR, input_type="chips", done=True,
        )
    if state == -3:
        return BotReply(_ghq_intro(session["data"]), quick_replies=YES_NO_QR, input_type="chips")
    if 0 <= state < len(flow["steps"]):
        data = session["data"]
        # بازیابی سشن وسطِ پرسش نام ثبت‌کننده → همان متن قاعده‌ای (بدون LLM)
        if data.get("_need_initiator"):
            return BotReply(_INITIATOR_ASK_TEXT, quick_replies=[])
        # data لازم است تا برچسب‌های «برای دیگری» (تلفن/سن/مراجع) درست بیاید
        return _step_prompt(flow, state, None, data)
    return BotReply(flow.get("welcome") or "سلام! خوش آمدید.")


# ── نقطه ورود اصلی ──

def handle_message(token: str, text: str) -> BotReply:
    """نقطهٔ ورود پیام کاربر.

    پلن «متوسط» روی قطعی LLM گفتگو را متوقف می‌کند (Q22) — به‌جای stack trace
    یا سؤالِ خشکِ قاعده‌ای، پیامِ فارسی برمی‌گردد و سشن دست‌نخورده می‌ماند تا
    کاربر بعد از برگشت سرویس ادامه دهد."""
    # بودجهٔ زمانی مشترک همهٔ فراخوانی‌های LLMِ این پیام (interpret + chat_ask)
    # → مجموع‌شان زیر سقف ۱۲۰ ثانیهٔ nginx می‌ماند و retryها با مکث پخش می‌شوند.
    llm_flow.llm_service.begin_request_budget()
    try:
        return _handle_message(token, text)
    except _ChatOutage:
        # ترنسکریپت ادمین هم باید توقف را ببیند (مسیر interpret خودش لاگ می‌کند
        # و return می‌کند؛ اینجا فقط مسیرهای raise‌شده می‌آیند → بدون دوباره‌لاگ)
        try:
            log_message(token, "bot", llm_flow.OUTAGE_MSG)
        except Exception:
            logger.exception("failed to log outage reply token=%s", token)
        return _outage_reply()
    finally:
        llm_flow.llm_service.end_request_budget()


def _handle_message(token: str, text: str) -> BotReply:
    session = get_session(token)
    if session is None:
        raise KeyError("session not found or expired")

    text = (text or "").strip()

    # فلوی مشتری (سشن مشتری غیرفعال/حذف‌شده → خطای مخصوص)
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT flow_config, name, slug FROM tenants WHERE id=? AND active=1", (session["tenant_id"],)).fetchone()
    conn.close()
    if row is None:
        raise TenantDisabled()
    flow = tenants_mod._parse_flow_config(row[0])
    if row[1]:
        flow["_tenant_name"] = row[1]
    if row[2]:
        flow["_tenant_slug"] = row[2]

    # تنظیمات LLM پلن tenant (weak/بدون پلن → None)
    # پیکربندی ناقص: متوسط باید صریح قطعی بگوید (Q22 — طبق docstring خود llm_flow)،
    # پیشرفته به موتور قاعده‌ای fallback می‌کند؛ وانمودِ AI بودن ممنوع.
    try:
        llm_cfg = llm_flow.get_tenant_llm(session["tenant_id"])
    except llm_flow.LLMConfigError as e:
        logger.error("LLM config incomplete tenant=%s: %s", session["tenant_id"], e)
        st = plan_service.get_tenant_plan_state(session["tenant_id"])
        if st and st["plan"].get("mode") == plan_service.MODE_MEDIUM:
            raise _ChatOutage()
        llm_cfg = None
    # تاریخچه همین سشن برای context LLM — قبل از لاگ پیام جاری (Q27)
    history = _session_history(token) if llm_cfg else None

    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH]

    if not text:
        return _state_prompt(session)

    log_message(token, "user", text)

    if text in RESTART_WORDS:
        return _restart(session, flow, llm_cfg, history)

    # دکمهٔ «تلاش مجدد» (پیام قطعی): سؤالِ جاری را دوباره بپرس — باید قبل از
    # همهٔ شاخه‌هایی باشد که متن را به‌عنوان پاسخ مصرف می‌کنند (initiator/استپ‌ها)
    if text in RETRY_WORDS:
        return _reask_current(session, flow, llm_cfg, history)

    state = session["state"]

    # پیام بعد از «برای دیگری»: نام ثبت‌کننده را بگیر و به همان مرحله بعدی برگرد (Q17)
    if session["data"].get("_need_initiator") and state >= 0:
        data = dict(session["data"])
        data["initiator_name"] = text[:120]
        data.pop("_need_initiator", None)
        # پرسش اول ساخته شود، بعد ذخیره — تا قطعی LLM سشن را دست‌نخورده
        # بگذارد و کاربر همان پیام را دوباره بفرستد (همان invariant مسیر relationship)
        prompt = _step_prompt(flow, state, llm_cfg, data, history)
        _save_session(token, state, data)
        log_message(token, "bot", prompt.text)
        return prompt

    # ربات: بعد از پایان، هر متنی = شروع مجدد (رفع باگ — عین main.py fallback)
    if state == -2:
        return _restart(session, flow, llm_cfg, history)
    if state == -3:
        # پرسش قبولی GHQ — بله/خیر — بدون فهم/لحن LLM روی متن GHQ (Q27)
        if text not in ("بله", "خیر"):
            return BotReply(
                "لطفاً یکی از گزینه‌های «بله» یا «خیر» را انتخاب کنید:",
                quick_replies=YES_NO_QR, input_type="chips",
            )
        if text == "بله":
            data = session["data"]
            data["ghq_answers"] = []
            data["ghq_index"] = 0
            _save_session(token, -1, data)
            qtext = _ghq_question_text(0)
            log_message(token, "bot", md_to_text(qtext))
            return BotReply(qtext, quick_replies=_ghq_quick_replies(0), input_type="chips+text")
        # GHQ رد شد: مچینگ/لحن پایانی از پلن پیروی کند
        return _finish(session, flow, llm_cfg=llm_cfg, history=history)
    if state == -1:
        return _handle_ghq(session, flow, text, llm_cfg)  # GHQ: بدون LLM
    if state >= len(flow["steps"]):
        return _restart(session, flow, llm_cfg, history)
    return _handle_step(session, flow, text, llm_cfg, history)
