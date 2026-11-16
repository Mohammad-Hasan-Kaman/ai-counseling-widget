# -*- coding: utf-8 -*-
"""کلاینت OpenAI-compatible: هر ارائه‌دهنده‌ای با base_url + api_key + model (تصمیم Q9).
تلاش مجدد فقط برای خطای گذرا (timeout/شبکه) — تعداد و سقفِ تکرار با پارامترهای
`retries` و `retry_timeout` کنترل می‌شود (مسیرهای وسط چت ۵ تلاش می‌زنند)."""
import httpx
import time

DEFAULT_TIMEOUT = 20.0
RETRY_TIMEOUT = 8.0
TEST_TIMEOUT = 10.0
MODELS_TIMEOUT = 12.0

# مکث بین تلاش‌ها — قفل سهمیهٔ 9router مدل‌به‌مدل ۸۰ تا ۱۰۴ ثانیه است؛ تلاش‌های
# پشت‌سرهم همه داخل همان قفل می‌افتند. فقط وقتی بودجهٔ درخواست فعال است اعمال
# می‌شود (تست‌ها بودجه ندارند → رفتار قدیمی، بدون مکث).
BACKOFF_DELAYS = (1.0, 2.0, 4.0, 6.0)

# بودجهٔ زمانی مشترکِ همهٔ فراخوانی‌های LLMِ یک پیام (دو تا: interpret + chat_ask).
# زیر سقف proxy_read_timeout=120s نگه داشته می‌شود تا nginx اتصال را نبُرد.
REQUEST_BUDGET_S = 360.0
# حداقل بودجهٔ لازم برای شروع یک تلاشِ دیگر (هر تلاش تا ۳۰s طول می‌کشد)
MIN_ATTEMPT_BUDGET_S = 12.0

_budget = {"deadline": None}


def begin_request_budget(seconds: float = REQUEST_BUDGET_S) -> None:
    """شروع بودجهٔ زمانی برای یک درخواست HTTP (در ورودی handle_message)."""
    _budget["deadline"] = time.monotonic() + seconds


def end_request_budget() -> None:
    """پایان بودجه — همیشه باید در finally صدا زده شود."""
    _budget["deadline"] = None


def budget_remaining() -> float | None:
    """بودجهٔ باقی‌مانده به ثانیه؛ None اگر بودجه‌ای فعال نیست."""
    d = _budget["deadline"]
    if d is None:
        return None
    return max(0.0, d - time.monotonic())


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _completions_url(base_url: str) -> str:
    return (base_url or "").rstrip("/") + "/chat/completions"


def _models_url(base_url: str) -> str:
    return (base_url or "").rstrip("/") + "/models"


def _post(base_url: str, api_key: str, model: str, messages: list, timeout: float) -> str:
    if not base_url or not api_key or not model:
        # status غیر-None تا تکرار نشود — پیکربندیِ خراب با هیچ تلاشی درست نمی‌شود
        raise LLMError("تنظیمات LLM (base_url / api_key / model) ناقص است.", status=422)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": 0.3}
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            resp = client.post(_completions_url(base_url), json=payload, headers=headers)
    except httpx.TimeoutException:
        raise LLMError(f"پاسخ LLM بیش از {timeout:g} ثانیه طول کشید (timeout).")
    except httpx.HTTPError as e:
        raise LLMError(f"خطای اتصال به LLM: {e.__class__.__name__}")
    if resp.status_code == 401 or resp.status_code == 403:
        raise LLMError("API key نامعتبر یا دسترسی غیرمجاز (401/403).", status=resp.status_code)
    if resp.status_code == 404:
        raise LLMError("مسیر یا مدل یافت نشد (404) — base_url/model را بررسی کنید.", status=404)
    if resp.status_code == 429:
        raise LLMError("محدودیت نرخ ارائه‌دهنده LLM (429).", status=429)
    if resp.status_code >= 400:
        raise LLMError(f"خطای LLM (HTTP {resp.status_code}): {resp.text[:200]}", status=resp.status_code)
    try:
        data = resp.json()
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except ValueError as e:
        # برخی درگاه‌ها JSON معیوب با دادهٔ اضافی برمی‌گردانند (Extra data) —
        # اولین آبجکت JSON را جدا کن و ادامه بده تا قطعی کاذب ندهیم
        import json as _json
        txt = (resp.text or "").strip()
        depth=0; end=-1; start=txt.find("{")
        if start >= 0:
            for i,ch in enumerate(txt[start:], start=start):
                if ch=="{": depth+=1
                elif ch=="}":
                    depth-=1
                    if depth==0:
                        end=i; break
        if start>=0 and end>start:
            try:
                data=_json.loads(txt[start:end+1])
                choice=data["choices"][0]
                message=choice.get("message") or {}
            except Exception:
                raise e
        else:
            raise e
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"پاسخ LLM فرمت معتبر ندارد: {e}")
    content = message.get("content")
    if not content:
        # finish_reason='tool_calls' یا 'length' یعنی content خالی است. پیش‌تر
        # اینجا "" برمی‌گشت و لایهٔ بالا آن را «پاسخِ موفقِ خالی» می‌پنداشت؛
        # نتیجه: خروجیِ JSON نشکسته می‌شد و به مراجع پیامِ قطعیِ AI نشان داده
        # می‌شد، در حالی که سرویس سالم بود و جواب داشت.
        raise LLMError(
            f"پاسخ LLM خالی است (finish_reason="
            f"{choice.get('finish_reason')!r}).", status=None)
    return content


def list_models(base_url: str, api_key: str,
                timeout: float = MODELS_TIMEOUT) -> tuple[list[str], str]:
    """مدل‌های در دسترسِ همین درگاه — GET {base}/models با همان Bearer.

    خروجی: (فهرست مرتبِ شناسه‌ها، پیام خطا). خطا یا فهرست خالی خطا نیست:
    ارائه‌دهنده ممکن است /models نداشته باشد؛ پس هیچ‌وقت استثنا پرتاب نمی‌کند
    و UI خودش تصمیم می‌گیرد — مدل را می‌شود دستی هم نوشت."""
    if not base_url or not api_key:
        return [], "برای دریافت مدل‌ها، base_url و API key لازم است."
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            resp = client.get(_models_url(base_url), headers=headers)
    except httpx.TimeoutException:
        return [], f"پاسخ درگاه بیش از {timeout:g} ثانیه طول کشید (timeout)."
    except httpx.HTTPError as e:
        return [], f"خطای اتصال به درگاه: {e.__class__.__name__}"
    if resp.status_code in (401, 403):
        return [], "API key نامعتبر یا دسترسی غیرمجاز (401/403)."
    if resp.status_code == 404:
        return [], "درگاه مسیر /models ندارد (404) — مدل را دستی وارد کنید."
    if resp.status_code >= 400:
        return [], f"خطای درگاه (HTTP {resp.status_code}): {resp.text[:200]}"
    try:
        data = resp.json()
    except ValueError:
        return [], "پاسخ درگاه JSON معتبر نیست."
    # OpenAI-compatible: {"data":[{"id":...}]}. بعضی درگاه‌ها آرایهٔ خام می‌دهند.
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], "ساختار پاسخ /models قابل شناسایی نیست."
    ids = []
    for it in items:
        if isinstance(it, str):
            mid = it.strip()
        elif isinstance(it, dict):
            mid = str(it.get("id") or it.get("name") or "").strip()
        else:
            continue
        if mid:
            ids.append(mid)
    return sorted(set(ids)), ""


def chat_completion(base_url: str, api_key: str, model: str, messages: list,
                    timeout: float = DEFAULT_TIMEOUT, retries: int = 1,
                    retry_timeout: float | None = None) -> str:
    """یک درخواست چت؛ خطای گذرا (timeout/شبکه) تا `retries` بار دیگر تکرار می‌شود.

    تلاش اول با `timeout`، تکرارها با `retry_timeout` (پیش‌فرض RETRY_TIMEOUT).
    خطای HTTP (کلید/مدل/سقف نرخ) اصلاً تکرار نمی‌شود — تکرارشان فقط هزینه است."""
    rt = RETRY_TIMEOUT if retry_timeout is None else retry_timeout
    attempt = 0
    while True:
        # بودجهٔ درخواست: قبل از هر تلاشِ دیگر اگر وقت کافی نمانده، تلاشِ ناقص
        # نزنیم (هم timeout می‌خورد هم بودجهٔ پیامِ دوم را می‌سوزاند).
        rem = budget_remaining()
        if attempt > 0 and rem is not None and rem < MIN_ATTEMPT_BUDGET_S:
            raise LLMError(
                f"بودجهٔ زمانی پیام تمام شد ({rem:.0f}s باقی مانده) — تلاشِ دیگر ممکن نیست.",
                status=503,
            )
        try:
            this_timeout = timeout if attempt == 0 else rt
            if rem is not None:
                this_timeout = max(1.0, min(this_timeout, rem))
            return _post(base_url, api_key, model, messages, this_timeout)
        except LLMError as e:
            # خطاهای گذرا (۵۰۰/۵۰۲/۵۰۳/۵۰۴) تکرار می‌شوند؛ خطای کلید/مدل/سقف
            # نرخ (۴xx) تکرار نمی‌شود چون تکرارشان فقط هزینه است.
            transient = e.status in (500, 502, 503, 504)
            if (e.status is not None and not transient) or attempt >= retries:
                raise
            attempt += 1
            # مکث بین تلاش‌ها — فقط وقتی بودجهٔ درخواست فعال است.
            rem = budget_remaining()
            if rem is not None and rem > 0:
                want = BACKOFF_DELAYS[min(attempt - 1, len(BACKOFF_DELAYS) - 1)]
                time.sleep(min(want, max(0.0, rem - MIN_ATTEMPT_BUDGET_S)))


def test_connection(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """برای دکمه «تست اتصال» پنل سوپرادمین (تصمیم Q39)"""
    try:
        chat_completion(
            base_url, api_key, model,
            [{"role": "user", "content": "ping"}],
            timeout=TEST_TIMEOUT, retries=0,
        )
        return True, "✅ اتصال برقرار شد — base_url، کلید و مدل معتبر هستند."
    except LLMError as e:
        return False, f"❌ {e}"
