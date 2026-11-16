# -*- coding: utf-8 -*-
"""پاسخ‌های واقعیِ LLM که لایهٔ بالا آن‌ها را «قطعی سرویس» می‌پنداشت.

هر دو نمونه از پاسخِ واقعیِ سرویس گرفته شده‌اند:
- content خالی با finish_reason='tool_calls' (متن در reasoning_content بود)
- JSON در میانِ توضیحِ متنی («**Mapping result:**\n\n{"value": "بله"}»)
"""
import pytest

from app import llm_flow, llm_service


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._p


def _post(payload, status=200):
    def fake(url, json=None, headers=None, **kw):
        fake.last = json
        return _Resp(payload, status)
    fake.last = None
    return fake


# ── لایهٔ پایین: content خالی نباید موفق شمرده شود ──

def test_empty_content_is_an_error_not_empty_string(monkeypatch):
    """finish_reason='tool_calls' → content=None.

    پیش‌تر "" برمی‌گشت؛ لایهٔ بالا آن را پاسخِ موفق می‌گرفت، JSON نشکسته
    می‌شد و مراجع پیامِ «دستیار هوشمند در دسترس نیست» می‌دید."""
    payload = {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "reasoning_content": "The user input means...",
        "tool_calls": [{"id": "1", "type": "function", "function": {
            "name": "x", "arguments": "{}"}}]}}]}
    monkeypatch.setattr(llm_service.httpx, "Client",
                        lambda **kw: type("C", (), {
                            "__enter__": lambda s: s, "__exit__": lambda *a: None,
                            "post": staticmethod(_post(payload).post if False else
                                                (lambda url, **k: _Resp(payload)))})())

    with pytest.raises(llm_service.LLMError) as ei:
        llm_service._post("http://x/v1", "k", "m", [{"role": "user", "content": "hi"}], 5)
    assert "خالی" in str(ei.value)
    # retryable باشد تا یک تلاشِ دیگر انجام شود
    assert ei.value.status is None


def test_real_content_still_passes_through(monkeypatch):
    payload = {"choices": [{"finish_reason": "stop",
                            "message": {"role": "assistant", "content": '{"value": "بله"}'}}]}
    monkeypatch.setattr(llm_service.httpx, "Client",
                        lambda **kw: type("C", (), {
                            "__enter__": lambda s: s, "__exit__": lambda *a: None,
                            "post": lambda s, url, **k: _Resp(payload)})())
    out = llm_service._post("http://x/v1", "k", "m", [{"role": "user", "content": "hi"}], 5)
    assert out == '{"value": "بله"}'


# ── لایهٔ بالا: JSON در میانِ متن ──

@pytest.mark.parametrize("raw,expected", [
    ('{"value": "بله"}', {"value": "بله"}),
    ('```json\n{"value": "بله"}\n```', {"value": "بله"}),
    ('```\n{"value": "بله"}\n```', {"value": "بله"}),
    ('**Mapping result:**\n\n{"value": "بله"}', {"value": "بله"}),
    ('نتیجه:\n```json\n{"value": null}\n```\nموفق بود.', {"value": None}),
    ('سلام\n{"recommendations": [{"name": "x", "reason": "y"}]}\nپایان',
     {"recommendations": [{"name": "x", "reason": "y"}]}),
    ('بدون JSON اصلاً', None),
    ('[1, 2, 3]', None),          # فهرست است، نه شیء
    ('', None),
    ('{"value": "بله"', None),    # JSON ناقص
])
def test_extract_json(raw, expected):
    assert llm_flow._extract_json(raw) == expected


def test_interpret_choice_survives_preamble(monkeypatch):
    """پاسخِ واقعی با پیشوندِ متنی باید نگاشت شود، نه اینکه outage بدهد."""
    monkeypatch.setattr(llm_flow, "_llm",
                        lambda cfg, msgs, **kw: '**Mapping result:**\n\n{"value": "بله"}')
    val, outage = llm_flow.interpret_choice(
        {"model": "m", "base_url": "u", "api_key": "k"},
        "بله همون که گفتی رو میخوام", ["بله", "خیر", "نمی‌دانم"])
    assert (val, outage) == ("بله", False)


def test_llm_match_survives_preamble(monkeypatch):
    monkeypatch.setattr(llm_flow, "_llm", lambda cfg, msgs, **kw: (
        'خروجی:\n```json\n{"recommendations": [{"name": "دکتر مریم احمدی", '
        '"reason": "تطابق"}]}\n```'))
    profiles = [{"clean_name": "دکتر مریم احمدی", "gender": "زن",
                 "location": "تهران", "active_specs": [], "age_range": ""}]
    recs, outage = llm_flow.llm_match(
        {"model": "m", "base_url": "u", "api_key": "k"},
        {"topic": "اضطراب"}, profiles, top_n=2, tenant_id=None)
    assert outage is False
    assert [r["name"] for r in recs] == ["دکتر مریم احمدی"]


def test_parse_failure_is_still_an_outage(monkeypatch):
    """پیش‌بینیِ نامعتبر باید همچنان outage بدهد تا مسیرِ fallback کار کند"""
    monkeypatch.setattr(llm_flow, "_llm", lambda cfg, msgs, **kw: "اصلاً JSON نیست")
    val, outage = llm_flow.interpret_choice(
        {"model": "m", "base_url": "u", "api_key": "k"}, "سلام", ["بله", "خیر"])
    assert (val, outage) == (None, True)
