# -*- coding: utf-8 -*-
"""لایه هیبرید LLM برای فلو (تصمیمات Q8/Q12/Q22/Q27/Q32/Q43).

- weak: بدون LLM (get_tenant_llm → None)
- medium: فقط LLM — خطای LLM = پیام فارسی، بدون fallback به قاعده (به‌جز اعتبارسنجی تلفن/GHQ که همیشه قاعده‌ای)
- advanced: LLM با fallback به موتور قاعده‌ای
- GHQ و تلفن هرگز به LLM نمی‌روند (Q27)
- خروجی مچینگ باید grounding روی نام‌های واقعی داشته باشد (Q12)
"""
import json
import logging
import re

from app import plan_service
from app import llm_service
from app.internal_ai_engine import names_match

logger = logging.getLogger(__name__)

# سقف کاندیداهای LLM (کنترل توکن) — با لاگ هنگام برش تا نامرئی نباشد
CANDIDATE_CAP = 60

# برای مسیرهایی که خطایشان = پیام قطعی وسط چت (interpret/chat_ask/مچینگ):
# درگاه‌های کند (مدلِ محلی) اغلب ۱۰-۲۰ ثانیه جواب می‌دهند و با سقف ۱۵ ثانیه،
# پاسخِ سالمِ کند هم «قطعی» نشان داده می‌شد. صبر بیشتر بهتر از قطعیِ دروغین است.
CHAT_TIMEOUT = 80.0
# تا ۵ تلاش قبل از اعلام قطعی (خواستهٔ محصول): خطای گذرا (تایم‌اوتِ لحظه‌ای،
# صف شدن در درگاه محلی) در تلاش‌های بعدی جواب می‌دهد؛ فقط قطعِ واقعی ۵ بار شکست
# می‌خورد. تکرارها سقف کوتاه‌تر دارند تا مجموع انتظار بی‌انتها نشود.
CHAT_RETRIES = 4          # + تلاش اول = ۵
CHAT_RETRY_TIMEOUT = 70.0

OUTAGE_MSG = (
    "در حال حاضر دستیار هوشمند موقتاً در دسترس نیست. "
    "لطفاً کمی بعد دوباره تلاش کنید."
)

# تنظیم LLM ناقص (پلن LLM‌دار با base_url/model/key خالی) با «قطعی سرویس» فرق دارد:
# پلن medium باید صریح به مراجع بگوید، نه اینکه بی‌صدا به موتور قاعده‌ای سقوط کند —
# وگرنه مشتریِ «مدل دلخواه» تا ابد یک پلن LLM‌دارِ خراب را بی‌خبر می‌بیند.
class LLMConfigError(llm_service.LLMError):
    """پیکربندی LLM پلن ناقص است — تقصیر مرکز نیست."""


def get_tenant_llm(tenant_id: int) -> dict | None:
    """تنظیمات LLM تخصیص‌یافته به tenant؛ None فقط برای weak/بدون پلن.

    پیکربندی اختصاصیِ خودِ مشتری (مهاجرت ۱۰) اگر کامل باشد مقدم است، وگرنه
    ارث از پلن. «کامل» یعنی هر سه بخش: نیمه‌کاره نباید جایِ ارث را بگیرد و
    مشتریِ فعال را بی‌صدا از کار بیندازد.

    خطای دیتابیس آگاهانه propagate می‌شود (نباید بی‌صدا به weak سقوط کند — Q22).
    پیکربندی ناقص LLMConfigError می‌دهد تا «متوسط» پیام بدهد و «پیشرفته»
    fallback کند — نه اینکه وانمود کند AI دارد.
    """
    state = plan_service.get_tenant_plan_state(tenant_id)
    if not state:
        return None
    plan = state["plan"]
    if plan["mode"] == plan_service.MODE_WEAK:
        return None
    own = state.get("tenant") or {}
    own_url = (own.get("llm_base_url") or "").strip()
    own_model = (own.get("llm_model") or "").strip()
    try:
        own_key = plan_service.get_tenant_api_key(tenant_id)
    except Exception as e:
        logger.error("tenant LLM API key decrypt failed tenant=%s: %s", tenant_id, e)
        own_key = ""
    if own_url and own_model and own_key:
        cfg = {"mode": plan["mode"], "base_url": own_url,
               "api_key": own_key, "model": own_model}
    else:
        try:
            key = plan_service.get_decrypted_api_key(plan["id"])
        except Exception as e:
            logger.error("LLM API key decrypt failed tenant=%s: %s", tenant_id, e)
            key = ""
        cfg = {
            "mode": plan["mode"],
            "base_url": (plan["base_url"] or "").strip(),
            "api_key": (key or "").strip(),
            "model": (plan["model"] or "").strip(),
        }
    missing = [f for f in ("base_url", "model", "api_key") if not cfg[f]]
    if missing:
        raise LLMConfigError(
            f"پیکربندی LLM ناقص است ({', '.join(missing)}).", status=422)
    return cfg


def _llm(cfg: dict, messages: list, **kw) -> str:
    """نقطهٔ واحد فراخوانی LLM در فلو — علامت «پیام هوشمند» را قبل از صدا می‌زند
    (قبل از صدا: خطای LLM هم شمرده می‌شود، Q43).
    این‌جا نه در llm_service، تا test_connection هرگز شمرده نشود و
    تست‌های monkeypatch روی chat_completion دست‌نخورده بمانند."""
    plan_service.mark_ai_participation()
    return llm_service.chat_completion(
        cfg["base_url"], cfg["api_key"], cfg["model"], messages, **kw,
    )


def _extract_json(text: str) -> dict | None:
    """اولین شیء JSON را از پاسخ بیرون می‌کشد.

    مدل‌ها گاهی پیش از JSON توضیح می‌دهند («**Mapping result:**\n\n{…}»).
    json.loads روی کلِ متن در آن حالت می‌شکست و یک پاسخِ کاملاً درست به‌عنوان
    «قطعی سرویس» به مراجع نشان داده می‌شد."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.MULTILINE)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if start is None:
                start, depth = i, 1
            else:
                depth += 1
        elif ch == "}" and start is not None:
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start:i + 1])
                except ValueError:
                    return None
                return data if isinstance(data, dict) else None
    return None


def _chat_json(cfg: dict, system: str, user: str, history: list | None = None) -> tuple[dict | None, bool]:
    """درخواست JSON؛ خروجی: (dict|None, outage) — outage یعنی خطای LLM/شبکه/پارس"""
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-6:])
    messages.append({"role": "user", "content": user})
    try:
        raw = _llm(cfg, messages, timeout=CHAT_TIMEOUT,
                   retries=CHAT_RETRIES, retry_timeout=CHAT_RETRY_TIMEOUT)
    except llm_service.LLMError as e:
        logger.warning("LLM call failed: %s", e)
        return None, True
    return _extract_json(raw), False


# ── لحن‌بخشی (هر پیام، بدون کش — Q32 ب) ──

_TONE_SYSTEM = (
    "تو ویرایشگر لحن فارسی هستی. متن ربات پذیرش یک مرکز مشاوره را با لحنی طبیعی، "
    "صمیمانه و انسانی بازنویسی کن. فقط متن فارسی برگردان، بدون توضیح اضافه. "
    "معنی، اطلاعات، اعداد و قالب‌بندی markdown (بولد/لینک) را دقیقاً حفظ کن. "
    "به هیچ عنوان لینک یا محتوای جدید اضافه نکن."
)


def rewrite_tone(cfg: dict | None, text: str, history: list | None = None) -> str:
    """در هر خطایی همان متن اصلی برمی‌گردد (Q22 — خطا لحن → متن خام)"""
    if not cfg or not text or len(text) < 40:
        return text
    if "](" in text:  # لینک رزرو — دست نزن
        return text
    messages = [{"role": "system", "content": _TONE_SYSTEM}]
    if history:
        messages.extend(history[-6:])
    messages.append({"role": "user", "content": f"متن برای بازنویسی:\n{text}"})
    try:
        out = _llm(
            cfg, messages,
            timeout=llm_service.DEFAULT_TIMEOUT, retries=0,
        )
        out = (out or "").strip()
        return out if out else text
    except llm_service.LLMError:
        return text


# ── فهم ورودی استپ‌ها (تلفن و GHQ هرگز اینجا نمی‌آیند) ──

def interpret_choice(cfg: dict | None, text: str, options: list[str],
                     history: list | None = None) -> tuple[str | None, bool]:
    """متن آزاد → یکی از گزینه‌ها؛ خروجی: (مقدار|None, outage)"""
    if not cfg:
        return None, False
    system = (
        "ورودی کاربر در چت پذیرش را فقط و فقط به یکی از گزینه‌های داده‌شده نگاشت کن. "
        'خروجی فقط JSON: {"value": "..."} که value باید عیناً یکی از گزینه‌ها باشد. '
        "اگر قابل نگاشت نیست: {\"value\": null}"
    )
    user = f"گزینه‌ها: {json.dumps(options, ensure_ascii=False)}\nورودی کاربر: {text}"
    data, outage = _chat_json(cfg, system, user, history=history)
    if outage or not data:
        return None, True
    value = data.get("value")
    if isinstance(value, str) and value in options:
        return value, False
    return None, False  # نگاشت نشد ولی LLM سالم است


def interpret_number(cfg: dict | None, text: str,
                     history: list | None = None) -> tuple[int | None, bool]:
    """متن آزاد → عدد صحیح معتبر؛ خروجی: (عدد|None, outage)"""
    if not cfg:
        return None, False
    system = (
        "عدد صحیح موجود در ورودی کاربر را استخراج کن (مثلاً «بیست و سه سال» → 23، "
        "«۳۰» → 30). خروجی فقط JSON: {\"value\": <int>} یا {\"value\": null} اگر عددی نیست."
    )
    data, outage = _chat_json(cfg, system, f"ورودی: {text}", history=history)
    if outage or not data:
        return None, True
    value = data.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, False
    iv = int(value)
    if 0 < iv < 150:
        return iv, False
    return None, False


# ── گفتگوی انسان‌گونه: LLM سؤال را با لحن طبیعی می‌پرسد (نه فرم خشک) ──

_CHAT_SYSTEM = (
    "تو دستیار هوشمند پذیرش یک مرکز مشاوره هستی و داری با مراجع **گفتگو** می‌کنی، "
    "فرم پر نمی‌کنی. الان باید سؤالِ بعدی را بپرسی.\n\n"
    "قواعد سخت:\n"
    "1. فقط **یک** سؤال بپرس. هرگز دو سؤال را با «و» یا «و همچنین» به هم نچسبان.\n"
    "2. شماره سؤال نگو («سؤال اول»، «سوال ۳») و هرگز فهرست سؤال‌ها را لو نده.\n"
    "3. اگر مراجع همین الان اطلاعاتی را در حرفش داده، دوباره نپرس — تأییدش کن و "
    "به سؤال بعدی برو.\n"
    "4. **مخاطبِ سؤال باید کاملاً روشن باشد.** هرگز «شما»ی مبهم استفاده نکن؛ "
    "نامِ مخاطب را بیاور (مثلاً «سن رضا چند سال است؟») یا ضمیرش را درست انتخاب کن. "
    "راهنمای «مخاطب» در پیام فقط برای فهمِ توست: عیناً یا بازنویسی‌شده در متن نیاور "
    "و هرگز دربارهٔ پُرکردنِ فرم، ثبت‌نام یا فرآیندِ پذیرش حرف نزن — فقط خودِ سؤال "
    "را بپرس. دلیل و انگیزهٔ سؤال را هم توضیح نده.\n"
    "5. لحن: صمیمی، محترمانه، کوتاه (حداکثر سه جمله). از ایموجی و از فهرست استفاده نکن. "
        "اگر جنسیت مراجع معلوم است، خطاب را مطابق همان بگذار (خانم/جناب) و اگر نامعلوم "
        "است اصلاً خطاب جنسیتی استفاده نکن.\n"
    "6. هرگز مشاوره یا تشخیص پزشکی نده؛ فقط پذیرش و جمع‌آوری اطلاعات.\n\n"
    "خروجی: فقط متن فارسیِ همان یک پیام، بدون هیچ توضیح یا قالب اضافه."
)


def chat_ask(cfg: dict | None, question_brief: str, data: dict,
             tenant_name: str = "", history: list | None = None,
             has_options: bool = False) -> tuple[str | None, bool]:
    """پرسش بعدی را به زبان طبیعی می‌سازد. خروجی: (متن|None, outage)

    outage یعنی LLM در دسترس نبود (خطا/شبکه/پارس) — «متوسط» روی این توقف
    می‌کند و «پیشرفته» به متن قاعده‌ای می‌افتد (Q22).
    has_options: گزینه‌ها به شکل دکمه (چیپس) نمایش داده می‌شوند و نباید در متن بیایند."""
    if not cfg:
        return None, False
    context = {
        "collected": {k: v for k, v in (data or {}).items()
                      if not k.startswith("_") and k not in
                      ("ghq_answers", "ghq_index", "ghq_scores", "request_count")},
        "for_someone_else": _is_for_other_data(data or {}),
        "what_to_ask": question_brief,
        "center": tenant_name,
    }
    user = (
        "اطلاعاتی که تا الان داری:\n"
        f"{json.dumps(context, ensure_ascii=False, default=str)}\n\n"
        f"سؤالِ بعدی که باید بپرسی (با مخاطبش):\n{question_brief}\n\n"
        "همین سؤال را با کلامِ طبیعی و کاملاً روشن بپرس: مخاطبِ سؤال (چه کسی) "
        "باید از متن فهمیده شود؛ دلیل سؤال را توضیح نده."
    )
    if has_options:
        user += (
            "\n\nگزینه‌ها به شکل دکمه زیر پیام نمایش داده می‌شوند؛ "
            "فقط سؤال را بپرس و فهرست گزینه‌ها را در متن نیاور."
        )
    messages = [{"role": "system", "content": _CHAT_SYSTEM}]
    if history:
        messages.extend(history[-8:])
    messages.append({"role": "user", "content": user})
    try:
        # ۵ تلاش قبل از قطعی: خطای گذرا (تایم‌اوتِ لحظه‌ای) نباید زود
        # پیام «در دسترس نیست» نشان دهد؛ قطعِ واقعی بعد از ۵ بار می‌ایستد
        out = _llm(cfg, messages, timeout=CHAT_TIMEOUT,
                   retries=CHAT_RETRIES, retry_timeout=CHAT_RETRY_TIMEOUT)
    except llm_service.LLMError as e:
        logger.warning("chat_ask failed: %s", e)
        return None, True
    out = (out or "").strip()
    return (out, False) if out else (None, False)


def _is_for_other_data(data: dict) -> bool:
    rel = data.get("relationship") or ""
    return bool(rel) and rel != "برای خودم"


# ── مچینگ LLM‌محور با grounding (Q10/Q12) ──

def _approved_examples(tenant_id: int) -> list[dict]:
    """نمونه‌های تأییدشده سوپر ادمین برای few-shot پرامپت مچینگ (Q48 ج)"""
    try:
        from app.config import USER_RECORDS_DB
        from app.db import get_conn
        conn = get_conn(USER_RECORDS_DB)
        rows = conn.execute(
            "SELECT prompt_text, corrected_text FROM few_shot_examples "
            "WHERE tenant_id=? AND status='approved' AND example_type='match' "
            "ORDER BY id DESC LIMIT 10",
            (tenant_id,),
        ).fetchall()
        conn.close()
        return [{"context": r[0], "counselor": r[1]} for r in rows]
    except Exception:
        return []


def llm_match(cfg: dict | None, user_info: dict, profiles: list[dict],
              top_n: int = 2, tenant_id: int | None = None,
              history: list | None = None) -> tuple[list[dict] | None, bool]:
    """مچینگ فقط با LLM؛ نام‌ها باید از فهرست واقعی تأیید شوند.
    خروجی: (list[{name, reason}]|None, outage)"""
    if not cfg:
        return None, True
    safe_info = {k: v for k, v in user_info.items() if k != "ghq_scores"}  # GHQ هرگز به LLM (Q42/Q27)
    if len(profiles) > CANDIDATE_CAP:
        logger.warning(
            "llm_match: %d profiles exceed cap %d — truncating candidate pool",
            len(profiles), CANDIDATE_CAP,
        )
    cands = []
    for p in profiles[:CANDIDATE_CAP]:
        cands.append({
            "name": p.get("clean_name") or p.get("name", ""),
            "gender": p.get("gender", ""),
            "location": p.get("location", ""),
            "specs": ", ".join((p.get("active_specs") or [])[:8]),
            "age_range": p.get("age_range", ""),
        })
    if not cands:
        return None, False
    system = (
        "تو کارشناس پذیرش یک مرکز مشاوره هستی. بر اساس اطلاعات مراجع، حداکثر "
        f"{top_n} مشاور از فهرست کاندیدا انتخاب کن. "
        "نام مشاوران باید دقیقاً از روی نام‌های فهرست کاندیدا باشد (هیچ نامی از خودت نساز). "
        'خروجی فقط JSON: {"recommendations": [{"name": "...", "reason": "..."}]} '
        "با reason کوتاه و فارسی. اگر هیچ‌کس مناسب نیست: {\"recommendations\": []}"
    )
    examples = _approved_examples(tenant_id) if tenant_id else []
    if examples:
        system += "\n\nنمونه‌های تأییدشده از تجربه قبلی:\n" + json.dumps(
            examples, ensure_ascii=False
        )
    if user_info.get("is_for_other"):
        system += (
            "\n\nنکته: فرم برای شخص دیگری پر شده؛ مراجع همان full_name است و "
            "سن/جنسیت/موضوعِ ملاک، مربوط به اوست — نه ثبت‌کننده (initiator_name)."
        )
    user = (
        f"اطلاعات مراجع: {json.dumps(safe_info, ensure_ascii=False)}\n"
        f"کاندیداها: {json.dumps(cands, ensure_ascii=False)}"
    )
    data, outage = _chat_json(cfg, system, user, history=history)
    if outage or data is None:
        return None, True
    recs = data.get("recommendations")
    if not isinstance(recs, list):
        return None, True
    out = []
    for item in recs[:top_n]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        reason = str(item.get("reason") or "").strip()[:300]
        if not name:
            continue
        # grounding: فقط نام‌های واقعی (Q12)
        matched = None
        for p in profiles:
            clean = p.get("clean_name") or p.get("name", "")
            if clean and names_match(name, clean):
                matched = clean
                break
        if not matched:
            continue
        out.append({"name": matched, "reason": reason or "تطابق با شرایط مراجع"})
    return out, False
