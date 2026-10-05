# -*- coding: utf-8 -*-
"""
Web widget intake conversation state machine — generic per-tenant flow engine
Each tenant's flow is read from tenants.flow_config; the Nikravan flow is identical to the Bale bot.
"""
import json
import re
import uuid
import html as html_mod
from datetime import datetime, timedelta

from app.config import (
    USER_RECORDS_DB, MAPPING_JSON, WIDGET_SESSION_TTL, MAX_MESSAGE_LENGTH,
)
from app.db import get_conn
from app.internal_ai_engine import engine, record_learning_event
from app.user_db import validate_phone_and_get_count, save_user_consultation, is_suspicious_phone
from app.ghq_analyzer import GHQ_QUESTIONS, calculate_ghq_scores, get_ghq_message
from app import tenants as tenants_mod

RESTART_WORDS = ("شروع مجدد", "شروع", "/start")
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

RESTART_QR = ["شروع مجدد"]
YES_NO_QR = ["بله", "خیر"]

GHQ_INTRO = (
    "📋 **آزمون غربالگری سلامت عمومی (GHQ-28)**\n\n"
    "تکمیل این آزمون اختیاری است اما به سیستم کمک می‌کند شرایط شما را با دقت بالینی بسیار بالاتری تحلیل کند.\n\n"
    "آیا مایل هستید این تست کوتاه (۲۸ سؤال) را پاسخ دهید؟"
)


class TenantDisabled(Exception):
    """The session belongs to a disabled/deleted tenant — the web layer must return 403."""

# Standard user_requests keys — everything else is stored in custom_data
_SOURCE_KEYS = {"full_name", "phone", "gender", "age", "topic", "has_prev_therapy",
                "prev_detail", "expectation", "preferred_gender", "branch"}

_CHOICE_LABELS = {
    "بله": True, "خیر": False,
}


# ── Telegram Markdown → web text/HTML conversion ──

def md_to_text(s: str) -> str:
    s = s.replace("**", "")
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", s)
    return s


def md_to_html(s: str) -> str:
    escaped = html_mod.escape(s)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)

    def _link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        # Only http/https allowed — blocks javascript: and other dangerous schemes
        if not re.match(r"https?://", url, re.IGNORECASE):
            return label
        # _top: inside the widget iframe it takes the whole window (new tab if the browser allows,
        # otherwise it at least replaces the parent frame so the site page shows under the widget)
        return f'<a href="{html_mod.escape(url, quote=True)}" target="_top" rel="noopener">{label}</a>'

    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, escaped)
    escaped = escaped.replace("\n", "<br>")
    return escaped


_profile_url_cache: dict = {}
_profile_url_cache_mtime: float = 0.0


def _profile_url(name: str) -> str:
    """Counselor booking page link from the crawler mapping (fuzzy name matching + caching)."""
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
    return "https://nikravan.org/team/"


# ── GHQ option detection (verbatim from main.py:120-131) ──

def _parse_ghq_choice(text: str, opts: list) -> int | None:
    clean_text = text.strip().translate(PERSIAN_DIGITS)
    m = re.search(r"^(\d+)", clean_text)
    if m and int(m.group(1)) in range(len(opts)):
        return int(m.group(1))
    for i, opt in enumerate(opts):
        if opt in clean_text or clean_text in opt:
            return i
    m_any = re.search(r"(\d+)", clean_text)
    if m_any and int(m_any.group(1)) in range(len(opts)):
        return int(m_any.group(1))
    return None


# ── Bot reply structure ──

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


# ── Session ──

def _expires() -> str:
    return (datetime.utcnow() + timedelta(seconds=WIDGET_SESSION_TTL)).strftime("%Y-%m-%d %H:%M:%S")


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
        conn.execute(
            "INSERT INTO widget_messages (token, role, text, tenant_id) VALUES (?, ?, ?, (SELECT tenant_id FROM widget_sessions WHERE token=?))",
            (token, role, text, token),
        )
    except Exception:
        # Old database without the tenant_id column
        conn.execute(
            "INSERT INTO widget_messages (token, role, text) VALUES (?, ?, ?)",
            (token, role, text),
        )
    conn.commit()
    conn.close()


# ── Generic flow helpers ──

_STEP_DEFAULT_LABELS = {
    "full_name": "لطفاً نام و نام خانوادگی خود را وارد فرمایید:",
    "phone": "لطفاً شماره تماس خود را وارد کنید (مانند 09123456789):",
    "gender": "جنسیت شما:",
    "age": "سن شما (به عدد):",
    "topic": "لطفاً موضوع اصلی یا مشکلی که برای آن به مشاوره نیاز دارید را بنویسید:",
    "prev_therapy": "آیا سابقه مراجعه قبلی به روان‌شناس یا روان‌پزشک داشته‌اید؟",
    "prev_detail": "در صورت تمایل توضیح مختصری درباره درمان قبلی بنویسید (در غیر این صورت یک پیام کوتاه ارسال کنید):",
    "expectation": "انتظار یا رویکرد مدنظر شما از جلسات مشاوره چیست؟",
    "preferred_gender": "ترجیح می‌دهید جنسیت مشاور شما چه باشد؟",
    "branch": "کدام شعبه مرکز مشاوره خانواده نیک‌روان برای شما مناسب‌تر است؟",
}


def _step_prompt(flow: dict, step_idx: int) -> BotReply:
    """Prompt for step step_idx of the tenant's flow"""
    step = flow["steps"][step_idx]
    label = step.get("label") or _STEP_DEFAULT_LABELS.get(step["key"], f"لطفاً {step['key']} را وارد کنید:")
    stype = step["type"]
    if stype == "choice":
        return BotReply(label, quick_replies=list(step.get("options") or ["بله", "خیر"]), input_type="chips")
    return BotReply(label)


_LEGACY_COND_ALIASES = {"prev_therapy": "has_prev_therapy", "has_prev_therapy": "prev_therapy"}


def _conditional_satisfied(data: dict, cond: str | None) -> bool:
    """Whether a conditional step (e.g. prev_detail only when prev_therapy is yes) should be skipped"""
    if not cond:
        return True
    key, _, val = cond.partition("=")
    key, val = key.strip(), val.strip()
    actual = data.get(key)
    # Backward compatibility for legacy conditions (the key name was renamed)
    if actual is None and key in _LEGACY_COND_ALIASES:
        actual = data.get(_LEGACY_COND_ALIASES[key])
    # The stored value may be a boolean (yes/no → True/False)
    if isinstance(actual, bool):
        actual = "بله" if actual else "خیر"
    return str(actual) == val


def _advance_state(session: dict, flow: dict, current: int):
    """Advance to the next non-conditional step; returns: next step number, or 'ask_ghq', 'ghq' or 'end'"""
    steps = flow["steps"]
    idx = current + 1
    while idx < len(steps):
        step = steps[idx]
        # Skip conditional steps whose condition is not met
        if not _conditional_satisfied(session["data"], step.get("conditional")):
            idx += 1
            continue
        return idx
    if flow.get("ghq"):
        return "ask_ghq"
    return "end"


def _restart(session: dict, flow: dict) -> BotReply:
    welcome = flow.get("welcome") or "سلام! خوش آمدید."
    _save_session(session["token"], 0, {})
    log_message(session["token"], "bot", md_to_text(welcome))
    return BotReply(welcome)


# ── Processing each step's reply ──

def _handle_step(session: dict, flow: dict, text: str) -> BotReply:
    data = session["data"]
    step = flow["steps"][session["state"]]
    stype = step["type"]

    # Choice steps must always have options
    if stype == "choice" and not step.get("options"):
        step["options"] = ["بله", "خیر"]

    value = None

    if stype == "text":
        if len(text) < 1:
            return _step_prompt(flow, session["state"])
        value = text

    elif stype == "phone":
        phone = text.translate(PERSIAN_DIGITS)
        if not (phone.isdigit() and len(phone) == 11 and phone.startswith("09")):
            _invalid = BotReply("⚠️ شماره تماس نامعتبر است. لطفاً یک شماره ۱۱ رقمی معتبر با 09 وارد کنید:")
            log_message(session["token"], "bot", md_to_text(_invalid.text))
            return _invalid
        full_name = data.get("full_name", "")
        # Nobody is blocked; conflicts are reviewed in the admin panel
        is_valid, req_count, _ = validate_phone_and_get_count(phone, full_name, session["tenant_id"])
        data["phone"] = phone
        data["request_count"] = req_count
        data["phone_suspicious"] = is_suspicious_phone(phone)
        value = phone

    elif stype == "number":
        t = text.translate(PERSIAN_DIGITS)
        if not t.isdigit() or not (0 < int(t) < 150):
            return BotReply("لطفاً یک عدد معتبر وارد فرمایید:")
        value = int(t)

    elif stype == "choice":
        opts = list(step.get("options") or ["بله", "خیر"])
        if text not in opts:
            return BotReply("لطفاً یکی از گزینه‌های زیر را انتخاب کنید:", quick_replies=opts, input_type="chips")
        value = text

    # Store the value: standard key or custom
    key = step["key"]
    # Legacy conditional mapping (has_prev_therapy=yes) onto the current prev_therapy key
    cond = step.get("conditional") or ""
    if cond.partition("=")[0].strip() == "has_prev_therapy" and data.get("prev_therapy") in ("بله", "خیر"):
        data["has_prev_therapy"] = "بله" if data["prev_therapy"] == "بله" else "خیر"
    if key == "prev_therapy" and value in ("بله", "خیر"):
        data["has_prev_therapy"] = "بله" if value == "بله" else "خیر"
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

    nxt = _advance_state(session, flow, session["state"])

    if nxt == "end":
        return _finish(session, flow)
    if nxt == "ask_ghq":
        # GHQ opt-in question — same as the bot (the test is optional)
        _save_session(session["token"], -3, data)  # -3 = GHQ opt-in question
        log_message(session["token"], "bot", md_to_text(GHQ_INTRO))
        return BotReply(GHQ_INTRO, quick_replies=YES_NO_QR, input_type="chips")
    if nxt == "ghq":
        data["ghq_answers"] = []
        data["ghq_index"] = 0
        _save_session(session["token"], -1, data)  # -1 = GHQ in progress
        qtext = _ghq_question_text(0)
        log_message(session["token"], "bot", md_to_text(qtext))
        return BotReply(qtext, quick_replies=_ghq_quick_replies(0), input_type="chips+text")

    _save_session(session["token"], nxt, data)
    prompt = _step_prompt(flow, nxt)
    log_message(session["token"], "bot", prompt.text)

    # Returning-client welcome message — only after the phone number is recorded
    if stype == "phone" and data.get("request_count", 0) > 1:
        full_name = data.get("full_name", "")
        tenant_name = flow.get("_tenant_name") or "مرکز"
        welcome = f"🌹 خوش‌آمدید {full_name} عزیز! این **بار {data['request_count']}‌ام** است که در {tenant_name} در خدمت شما هستیم.\n\n"
        return BotReply(welcome + prompt.text, quick_replies=prompt.quick_replies, input_type=prompt.input_type)

    # Gentle warning for a fake-looking number (once, no blocking)
    if stype == "phone" and data.get("phone_suspicious"):
        data["phone_suspicious"] = False
        notice = "📝 شماره ثبت شد؛ لطفاً مطمئن شوید شماره‌ای است که کارشناسان مرکز بتوانند با شما تماس بگیرند.\n\n"
        return BotReply(notice + prompt.text, quick_replies=prompt.quick_replies, input_type=prompt.input_type)

    return prompt


def _handle_ghq(session: dict, flow: dict, text: str) -> BotReply:
    data = session["data"]
    idx = data.get("ghq_index", 0)
    if idx >= len(GHQ_QUESTIONS):
        return _finish(session, flow)
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
        return _finish(session, flow, ghq_prepend=ghq_msg)

    _save_session(session["token"], -1, data)
    qtext = _ghq_question_text(data["ghq_index"])
    log_message(session["token"], "bot", md_to_text(qtext))
    return BotReply(qtext, quick_replies=_ghq_quick_replies(data["ghq_index"]), input_type="chips+text")


# ── End of flow ──

def _finish(session: dict, flow: dict, ghq_prepend: str | None = None) -> BotReply:
    data = session["data"]
    custom = data.get("_custom")
    ending = flow.get("ending", "lead_capture")

    if ending == "recommend":
        reply = _recommend(session, flow)
    else:
        reply = _lead_capture(session, flow)

    if ghq_prepend:
        reply.text = ghq_prepend + "\n\n" + reply.text

    data.pop("_custom", None)
    _save_session(session["token"], -2, data)  # -2 = DONE
    log_message(session["token"], "bot", md_to_text(reply.text))
    return BotReply(reply.text, quick_replies=RESTART_QR, input_type="chips", done=True)


def _build_user_info(data: dict) -> dict:
    prev = data.get("has_prev_therapy", data.get("prev_therapy", False))
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
    }


def _recommend(session: dict, flow: dict) -> BotReply:
    data = session["data"]
    tenant_id = session["tenant_id"]
    user_info = _build_user_info(data)

    recs = engine.match(user_info, tenant_id)

    # Record the lead even when no counselor is found — the user's request must not be lost
    req_number = save_user_consultation(
        session["token"], user_info, recs, tenant_id, data.get("_custom")
    )

    if not recs:
        tenant_name = flow.get("_tenant_name") or "مرکز"
        badge = f" با شماره **{req_number}**" if req_number > 1 else ""
        return BotReply(
            "متأسفانه در حال حاضر مشاوری با این مشخصات یافت نشد؛ "
            f"اما درخواست شما{badge} در {tenant_name} ثبت شد و کارشناسان ما با شما تماس می‌گیرند.\n\n"
            "✨ برای ثبت درخواست جدید، گزینه «شروع مجدد» را انتخاب کنید."
        )

    ghq = user_info["ghq_scores"]
    ghq_lvl = "severe" if (ghq and ghq.get("total", 0) >= 43) else "normal"
    for r in recs:
        record_learning_event(r["name"], user_info["topic"], ghq_level=ghq_lvl, success=True, tenant_id=tenant_id)

    count_badge = f" (ثبت درخواست نوبت {req_number} شما)" if req_number > 1 else ""
    tenant_name = flow.get("_tenant_name") or "مرکز"
    msg = f"🎯 **مشاوران برگزیده {tenant_name} برای شما{count_badge}:**\n\n"
    for i, r in enumerate(recs, 1):
        msg += f"🏅 **گزینه {i}: {r['name']}**\n"
        msg += f"📋 دلیل پیشنهاد: {r['reason']}\n"
        url = _profile_url(r["name"]) if flow.get("_tenant_slug", "nikravan") == "nikravan" else None
        if url:
            msg += f"🔗 [مشاهده نوبت‌ها و رزرو در سایت]({url})\n"
        msg += "\n"

    msg += "✨ برای ارزیابی مجدد یا ثبت درخواست جدید، گزینه «شروع مجدد» را انتخاب کنید."
    return BotReply(msg)


def _lead_capture(session: dict, flow: dict) -> BotReply:
    data = session["data"]
    tenant_id = session["tenant_id"]
    user_info = _build_user_info(data)
    save_user_consultation(session["token"], user_info, [], tenant_id, data.get("_custom"))
    return BotReply(flow.get("thanks_text", "متشکریم! اطلاعات شما ثبت شد."))


# ── Session recovery (page refresh) ──

def _state_prompt(session: dict) -> BotReply:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT flow_config FROM tenants WHERE id=?", (session["tenant_id"],)).fetchone()
    conn.close()
    flow = tenants_mod._parse_flow_config(row[0] if row else None)

    state = session["state"]
    if state == 0:
        return BotReply(flow.get("welcome") or "سلام! خوش آمدید.")
    if state == -1:
        data = session["data"]
        if data.get("ghq_index") is not None and data.get("ghq_answers") is not None:
            idx = data["ghq_index"]
            if idx < len(GHQ_QUESTIONS):
                return BotReply(_ghq_question_text(idx), quick_replies=_ghq_quick_replies(idx), input_type="chips+text")
            # All answers were given but finish was never saved (old session) — return the same summary
            scores = calculate_ghq_scores(data["ghq_answers"])
            return BotReply(get_ghq_message(scores), quick_replies=RESTART_QR, input_type="chips", done=True)
        # GHQ not started yet — return to the current stage
        return _restart(session, flow)
    if state == -2:
        return BotReply(
            "✨ برای ارزیابی مجدد یا ثبت درخواست جدید، گزینه «شروع مجدد» را انتخاب کنید.",
            quick_replies=RESTART_QR, input_type="chips", done=True,
        )
    if state == -3:
        return BotReply(GHQ_INTRO, quick_replies=YES_NO_QR, input_type="chips")
    if 0 <= state < len(flow["steps"]):
        return _step_prompt(flow, state)
    return BotReply(flow.get("welcome") or "سلام! خوش آمدید.")


# ── Main entry point ──

def handle_message(token: str, text: str) -> BotReply:
    session = get_session(token)
    if session is None:
        raise KeyError("session not found or expired")

    text = (text or "").strip()

    # Tenant flow (session of a disabled/deleted tenant → dedicated error)
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

    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH]

    if not text:
        return _state_prompt(session)

    log_message(token, "user", text)

    if text in RESTART_WORDS:
        return _restart(session, flow)

    state = session["state"]
    # Bot: after the end, any text = restart (bug fix — same as main.py fallback)
    if state == -2:
        return _restart(session, flow)
    if state == -3:
        # GHQ opt-in question — yes/no
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
        return _finish(session, flow)
    if state == -1:
        return _handle_ghq(session, flow, text)
    if state >= len(flow["steps"]):
        return _restart(session, flow)
    return _handle_step(session, flow, text)
