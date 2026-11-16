# -*- coding: utf-8 -*-
"""تست‌های لایه LLM هیبرید — با mock کردن llm_service.chat_completion (بدون شبکه)"""
import json
import uuid

import pytest

from app import llm_flow, llm_service, plan_service, session_store as ss
from app import tenants as tenants_mod


@pytest.fixture()
def medium_tenant(dbs):
    """tenant با فلوی مرکز نمونه (seed شده id=1 در دیتابیس تست) + پلن متوسط"""
    suffix = uuid.uuid4().hex[:8]
    # فلوی کامل از seed: tenant id=1 همیشه مرکز نمونه با DEFAULT_DEMO_FLOW است
    pid = plan_service.create_plan(
        f"متوسط تست {suffix}", plan_service.MODE_MEDIUM,
        model="test-model", base_url="https://llm.example/v1", api_key="sk-test-key",
        message_quota=-1, introduction_quota=-1,
    )
    plan_service.assign_plan(1, pid, "tester")
    return {"tenant_id": 1, "plan_id": pid, "api_key": _api_key(1)}


def _api_key(tenant_id: int) -> str:
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT api_key FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    conn.close()
    return row[0]


# ── پیکربندی ناقصِ پلن LLM‌دار ──

def test_incomplete_llm_config_raises_config_error(dbs):
    """پلن LLM‌دار با model خالی نباید بی‌صدا وانمود کند هوش مصنوعی دارد.

    پیش از این، get_tenant_llm یک cfg ناقص برمی‌گرداند؛ فراخوانی می‌شد، خطای
    HTTP می‌خورد و «پیشرفته» بی‌سروصدا به موتور قاعده‌ای می‌افتاد — مشتریِ
    «مدل دلخواه» ماه‌ها فکر می‌کرد مدلش کار می‌کند."""
    # فرم اجازهٔ مدل خالی نمی‌دهد → ردیف را مستقیم در دیتابیس می‌سازیم

    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    from app import encryption_service
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.execute(
        "INSERT INTO plans (name, mode, model, base_url, api_key_encrypted, "
        "message_quota, introduction_quota) VALUES (?,?,?,?,?,?,?)",
        ("ناقص تست", plan_service.MODE_MEDIUM, "  ", "https://llm.example/v1",
         encryption_service.encrypt_str("sk-test-key"), -1, -1),
    )
    conn.commit()
    broken = cur.lastrowid
    conn.close()
    plan_service.assign_plan(1, broken, "tester")

    with pytest.raises(llm_flow.LLMConfigError) as e:
        llm_flow.get_tenant_llm(1)
    assert "model" in str(e.value)
    # زیرکلاس LLMError است → handlerهای موجودِ «متوسط» آن را می‌گیرند
    assert isinstance(e.value, llm_service.LLMError)


def test_incomplete_llm_config_does_not_break_chat(dbs):
    """همان پلن خراب، چت را نمی‌ترکاند — به موتور قاعده‌ای پایه می‌افتد"""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.execute(
        "INSERT INTO plans (name, mode, model, base_url, api_key_encrypted, "
        "message_quota, introduction_quota) VALUES (?,?,?,?,?,?,?)",
        ("ناقص چت", plan_service.MODE_ADVANCED, "", "https://llm.example/v1", "", -1, -1),
    )
    conn.commit()
    broken = cur.lastrowid
    conn.close()
    plan_service.assign_plan(1, broken, "tester")

    token = ss.create_session(1)
    ss.handle_message(token, "سلام")
    reply = ss.handle_message(token, "۳۰ ساله هستم و اضطراب دارم")
    assert reply.text  # جوابی برگشت — stack trace نه


def test_medium_incomplete_config_shows_outage(dbs):
    """پیکربندی ناقصِ «متوسط» = قطعی صریح با دکمهٔ تلاش مجدد — نه سکوت روی قاعده‌ای

    مشتریِ پلن متوسط باید بفهمد سرویسش خراب است؛ سقوط بی‌صدا به موتور
    قاعده‌ای یعنی ماه‌ها بی‌خبر از پیکربندیِ ناقصش می‌ماند (Q22)."""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    from app import encryption_service
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.execute(
        "INSERT INTO plans (name, mode, model, base_url, api_key_encrypted, "
        "message_quota, introduction_quota) VALUES (?,?,?,?,?,?,?)",
        ("ناقص متوسط", plan_service.MODE_MEDIUM, "  ", "https://llm.example/v1",
         encryption_service.encrypt_str("sk-test-key"), -1, -1),
    )
    conn.commit()
    broken = cur.lastrowid
    conn.close()
    plan_service.assign_plan(1, broken, "tester")

    token = ss.create_session(1)
    reply = ss.handle_message(token, "برای خودم")
    assert llm_flow.OUTAGE_MSG in reply.text
    assert "تلاش مجدد" in reply.quick_replies
    assert ss.get_session(token)["state"] == 0  # سشن دست‌نخورده — پاسخ مصرف نشد


def test_initiator_capture_outage_keeps_need_flag(dbs, medium_tenant, spy):
    """نام ثبت‌کننده: پرسش بعدی اول ساخته می‌شود، بعد ذخیره — قطعی نباید پاسخ را بسوزاند"""
    token = ss.create_session(1)
    spy(reply_text="بسیار خب.")
    ss.handle_message(token, "برای فرزندم")
    assert ss.get_session(token)["data"].get("_need_initiator")

    spy(error=True)  # LLM قطع شد
    reply = ss.handle_message(token, "مریم احمدی")
    assert llm_flow.OUTAGE_MSG in reply.text
    data = ss.get_session(token)["data"]
    assert data.get("_need_initiator")        # سشن دست‌نخورده — پیام دوباره می‌آید
    assert "initiator_name" not in data


class _Spy:
    def __init__(self, reply_json=None, reply_text=None, error=False):
        self.calls = []
        self.reply_json = reply_json
        self.reply_text = reply_text
        self.error = error

    def __call__(self, base_url, api_key, model, messages, **kw):
        self.calls.append({"messages": messages, "model": model})
        if self.error:
            raise llm_service.LLMError("timeout test")
        if self.reply_json is not None:
            return json.dumps(self.reply_json, ensure_ascii=False)
        return self.reply_text or ""


@pytest.fixture()
def spy(monkeypatch):
    def _install(**kwargs):
        s = _Spy(**kwargs)
        monkeypatch.setattr(llm_service, "chat_completion", s)
        # llm_flow وابسته به llm_service.chat_completion است — همان کافی است
        return s
    return _install


# ── پیکربندی ──

def test_get_tenant_llm_weak_and_missing(dbs):
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(f"ضعیف {suffix}", f"w_{suffix}", "WeakPass12!")
    pid = plan_service.create_plan("weak-p", plan_service.MODE_WEAK)
    plan_service.assign_plan(t["tenant_id"], pid, "tester")
    assert llm_flow.get_tenant_llm(t["tenant_id"]) is None

    t2 = tenants_mod.create_tenant(f"بدون‌پلن {suffix}", f"n_{suffix}", "NoPlanPass1!")
    assert llm_flow.get_tenant_llm(t2["tenant_id"]) is None


def test_get_tenant_llm_medium_decrypts_key(dbs, medium_tenant):
    cfg = llm_flow.get_tenant_llm(1)
    assert cfg is not None
    assert cfg["mode"] == plan_service.MODE_MEDIUM
    assert cfg["api_key"] == "sk-test-key"
    assert cfg["model"] == "test-model"


# ── لحن ──

def test_rewrite_tone_success_and_failure(dbs, spy, monkeypatch):
    s = spy(reply_text="متن لحن‌شده جدید")
    out = llm_flow.rewrite_tone({"base_url": "x", "api_key": "k", "model": "m"}, "متن اصلی به اندازه کافی بلند هست برای لحن‌بخشی آزمایشی")
    assert out == "متن لحن‌شده جدید"

    s2 = spy(error=True)
    original = "متن اصلی به اندازه کافی بلند هست برای لحن‌بخشی آزمایشی"
    out2 = llm_flow.rewrite_tone({"base_url": "x", "api_key": "k", "model": "m"}, original)
    assert out2 == original  # خطا → متن خام (Q22)

    # متن کوتاه یا حاوی لینک: بدون فراخوانی
    s3 = spy(reply_text="نباید صدا زده شود")
    assert llm_flow.rewrite_tone({"base_url": "x", "api_key": "k", "model": "m"}, "کوتاه") == "کوتاه"
    assert llm_flow.rewrite_tone({"base_url": "x", "api_key": "k", "model": "m"},
                                 "متن بلند [متن](https://example.com) اینجا هم ادامه دارد") == \
        "متن بلند [متن](https://example.com) اینجا هم ادامه دارد"
    assert len(s3.calls) == 0


# ── گفتگوی انسان‌گونه ──

def test_chat_ask_returns_natural_question(dbs, spy):
    s = spy(reply_text="خوب، ممنون از شما. حالا لطفاً بفرمایید چه مشکلی دارید؟")
    text, outage = llm_flow.chat_ask(
        {"base_url": "x", "api_key": "k", "model": "m"},
        "موضوع اصلی مشاوره", {"full_name": "علی"}, "مرکز مشاوره نمونه",
    )
    assert not outage
    assert "مشکلی" in text
    assert len(s.calls) == 1
    sent = json.dumps(s.calls[0]["messages"], ensure_ascii=False)
    assert "مرکز مشاوره نمونه" in sent  # مرکز در پرامپت می‌آید


def test_chat_ask_outage(dbs, spy):
    spy(error=True)
    text, outage = llm_flow.chat_ask(
        {"base_url": "x", "api_key": "k", "model": "m"}, "سن", {}, "مرکز مشاوره نمونه",
    )
    assert text is None and outage


def test_chat_ask_no_cfg_makes_no_call(dbs, spy):
    s = spy(reply_text="نباید صدا زده شود")
    text, outage = llm_flow.chat_ask(None, "سن", {}, "مرکز مشاوره نمونه")
    assert text is None and not outage
    assert len(s.calls) == 0


def test_medium_chat_outage_halts_conversation(dbs, medium_tenant, spy):
    """متوسط: قطع LLM یعنی توقف گفتگو، نه fallback قاعده‌ای (Q22)

    سشن باید دست‌نخورده بماند: پاسخِ کاربر مصرف نمی‌شود و بعد از برگشت
    سرویس دوباره ارسال خواهد شد."""
    token = ss.create_session(1)
    spy(reply_text="بسیار خب، لطفاً نام و نام خانوادگی‌ات را بنویس.")  # LLM سالم
    ss.handle_message(token, "برای خودم")   # → استپ نام
    ss.handle_message(token, "علی خاموشی")  # → استپ تلفن
    spy(error=True)                          # LLM قطع شد
    reply = ss.handle_message(token, "09129876545")  # تلفن معتبر → سؤال جنسیت باید ساخته شود
    assert llm_flow.OUTAGE_MSG in reply.text
    session = ss.get_session(token)
    assert session["state"] == 3            # جلو نرفت — سؤال جنسیت هرگز پرسیده نشد
    assert "phone" not in session["data"]   # پاسخ کاربر مصرف نشد
    # قطعی در ترنسکریپت ادمین هم ثبت شد (لاگِ wrapper)
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    logged = conn.execute(
        "SELECT COUNT(*) FROM widget_messages WHERE token=? AND role='bot' AND text LIKE '%در دسترس نیست%'",
        (token,),
    ).fetchone()[0]
    conn.close()
    assert logged >= 1


# ── فهم ورودی ──

def test_interpret_choice_maps_synonym(dbs, spy):
    spy(reply_json={"value": "خانم"})
    val, outage = llm_flow.interpret_choice(
        {"base_url": "x", "api_key": "k", "model": "m"},
        "من خانم هستم", ["آقا", "خانم"],
    )
    assert val == "خانم" and not outage


def test_interpret_choice_rejects_out_of_options(dbs, spy):
    spy(reply_json={"value": "بچه"})
    val, outage = llm_flow.interpret_choice(
        {"base_url": "x", "api_key": "k", "model": "m"},
        "ورودی", ["آقا", "خانم"],
    )
    assert val is None and not outage  # grounding: خارج از گزینه‌ها رد می‌شود


def test_interpret_choice_outage_flag(dbs, spy):
    spy(error=True)
    val, outage = llm_flow.interpret_choice(
        {"base_url": "x", "api_key": "k", "model": "m"}, "ورودی", ["بله", "خیر"],
    )
    assert val is None and outage


def test_interpret_number_persian_text(dbs, spy):
    spy(reply_json={"value": 25})
    val, outage = llm_flow.interpret_number(
        {"base_url": "x", "api_key": "k", "model": "m"}, "بیست و پنج ساله‌ام",
    )
    assert val == 25 and not outage


def test_interpret_number_out_of_range(dbs, spy):
    spy(reply_json={"value": 300})
    val, _ = llm_flow.interpret_number(
        {"base_url": "x", "api_key": "k", "model": "m"}, "سه‌صد",
    )
    assert val is None


# ── مچینگ grounded ──

def _profiles():
    return [
        {"clean_name": "مریم احمدی", "gender": "خانم", "location": "ظفر",
         "active_specs": ["اضطراب"], "age_range": "۲۰-۴۰"},
        {"clean_name": "علی رضایی", "gender": "آقا", "location": "هر دو",
         "active_specs": ["افسردگی"], "age_range": ""},
    ]


def test_llm_match_grounding_rejects_fake_name(dbs, spy):
    spy(reply_json={"recommendations": [
        {"name": "دکتر تخیلی", "reason": "مناسب"},
        {"name": "مریم احمدی", "reason": "تطابق موضوع"},
    ]})
    recs, outage = llm_flow.llm_match(
        {"base_url": "x", "api_key": "k", "model": "m"},
        {"age": 30, "topic": "اضطراب"}, _profiles(),
    )
    assert not outage
    assert recs is not None and len(recs) == 1
    assert recs[0]["name"] == "مریم احمدی"  # فقط نام واقعی قبول شد


def test_llm_match_strips_ghq_from_prompt(dbs, spy):
    s = spy(reply_json={"recommendations": []})
    llm_flow.llm_match(
        {"base_url": "x", "api_key": "k", "model": "m"},
        {"age": 30, "topic": "اضطراب", "ghq_scores": {"total": 50, "depression": 16}},
        _profiles(),
    )
    sent = json.dumps(s.calls[0]["messages"], ensure_ascii=False)
    assert "ghq" not in sent.lower()
    assert "50" not in sent  # نمره GHQ نباید به LLM برود (Q42)


def test_llm_match_outage(dbs, spy):
    spy(error=True)
    recs, outage = llm_flow.llm_match(
        {"base_url": "x", "api_key": "k", "model": "m"},
        {"age": 30, "topic": "x"}, _profiles(),
    )
    assert recs is None and outage


# ── یکپارچگی با فلو ──

def _fake_profiles(monkeypatch):
    from app.internal_ai_engine import engine
    monkeypatch.setattr(
        engine, "get_profiles",
        lambda tid, force=False: [
            {"clean_name": "مریم احمدی", "gender": "خانم", "location": "ظفر",
             "active_specs": ["اضطراب"], "age_range": "۲۰-۴۰",
             "age_min": 20, "age_max": 40},
            {"clean_name": "علی رضایی", "gender": "آقا", "location": "هر دو",
             "active_specs": ["افسردگی"], "age_range": "",
             "age_min": 0, "age_max": 120},
        ],
    )


def test_flow_medium_choice_and_age_via_llm(dbs, medium_tenant, spy, monkeypatch):
    """جنسیت با مترادف + سن متنی — هر دو با LLM، تلفن بدون LLM"""
    token = ss.create_session(1)
    spy(reply_text="بسیار خب.")  # LLM سالم — پیام‌های اولیه نباید به شبکهٔ واقعی بزنند
    # استپ رابطه (گزینه دقیق)
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "علی تست")  # نام → مرحله تلفن
    # تلفن: قاعده‌ای (بدون LLM حتی اگر خراب باشد)
    s = spy(reply_json={"value": 99})
    ss.handle_message(token, "0912")  # نامعتبر — نباید LLM بزند
    assert len(s.calls) == 0
    ss.handle_message(token, "09129876545")  # معتبر → جنسیت

    # جنسیت: مترادف → LLM (۱ فراخوانی interpret + بعداً لحنِ پرامپت بعدی)
    s2 = spy(reply_json={"value": "خانم"})
    reply = ss.handle_message(token, "من خانم هستم")
    assert len(s2.calls) >= 1
    first_system = s2.calls[0]["messages"][0]["content"]
    assert "گزینه" in first_system  # interpret_choice بود نه لحن
    assert ss.get_session(token)["state"] == 5  # رفت سر سن

    # سن متنی → LLM interpret + لحن پرامپت بعدی
    s3 = spy(reply_json={"value": 25})
    ss.handle_message(token, "بیست و پنج سالم است")
    assert len(s3.calls) >= 1
    assert "استخراج" in s3.calls[0]["messages"][0]["content"]
    session = ss.get_session(token)
    assert session["state"] == 6
    assert session["data"]["age"] == 25


def test_flow_medium_outage_shows_persian_error(dbs, medium_tenant, spy):
    token = ss.create_session(1)
    spy(reply_text="بسیار خب.")  # LLM سالم تا رسیدن به استپ جنسیت
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "علی")
    ss.handle_message(token, "09129876545")
    s = spy(error=True)
    reply = ss.handle_message(token, "من خانم هستم")  # انتخابی خارج از گزینه‌ها + LLM خراب
    assert llm_flow.OUTAGE_MSG in reply.text
    assert ss.get_session(token)["state"] == 4  # پیشرفت نکرد (Q22 متوسط) — روی جنسیت
    # پیام outage باید در ترنسکریپت ادمین هم ثبت شود
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    logged = conn.execute(
        "SELECT COUNT(*) FROM widget_messages WHERE token=? AND role='bot' AND text LIKE '%در دسترس نیست%'",
        (token,),
    ).fetchone()[0]
    conn.close()
    assert logged >= 1


def test_ghq_never_calls_llm(dbs, medium_tenant, spy, monkeypatch):
    """GHQ وسط تست: صفر فراخوانی LLM (Q27)"""
    token = ss.create_session(1)
    # دستی state را GHQ جاری کن
    conn_data = {"ghq_answers": [], "ghq_index": 0}
    ss._save_session(token, -1, dict(conn_data))
    s = spy(reply_text="نباید صدا زده شود")
    reply = ss.handle_message(token, "0")  # اولین گزینه GHQ
    assert len(s.calls) == 0
    assert ss.get_session(token)["data"]["ghq_index"] == 1
    assert "سؤال" in reply.text or "📝" in reply.text


def test_tone_applied_on_step_prompt(dbs, medium_tenant, spy):
    token = ss.create_session(1)
    s = spy(reply_text="سلام! لطفاً نام و نام خانوادگی‌ات رو بنویس.")
    reply = ss.handle_message(token, "شروع مجدد")
    # welcome تُن خورده + اولین سؤال فلو (وعدهٔ «سؤال‌های زیر» باید همان‌جا ادا شود)
    assert reply.text.startswith("سلام! لطفاً نام و نام خانوادگی‌ات رو بنویس.")
    assert reply.quick_replies  # گزینه‌های اولین سؤال هم آمده‌اند
    assert len(s.calls) >= 1


def test_few_shot_approved_examples_in_prompt(dbs, medium_tenant, spy):
    """نمونه‌های تأییدشده سوپر ادمین داخل پرامپت مچینگ می‌روند (Q48)"""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    conn.execute(
        "INSERT INTO few_shot_examples (tenant_id, example_type, prompt_text, corrected_text, status) "
        "VALUES (?,?,?,?, 'approved')",
        (1, "match", "اضطراب شدید در نوجوان", "مریم احمدی"),
    )
    conn.commit()
    conn.close()
    s = spy(reply_json={"recommendations": []})
    llm_flow.llm_match(
        {"base_url": "x", "api_key": "k", "model": "m"},
        {"age": 15, "topic": "اضطراب"}, _profiles(), tenant_id=1,
    )
    sent = json.dumps(s.calls[0]["messages"], ensure_ascii=False)
    assert "مریم احمدی" in sent  # نمونه وارد پرامپت شد


def test_session_history_passed_to_llm(dbs, medium_tenant, spy):
    """تاریخچه همین سشن به فراخوانی LLM می‌رود (Q27) — پیام‌های قبلی در messages"""
    token = ss.create_session(1)
    spy(reply_text="بسیار خب.")  # LLM سالم — پیاده‌روی اولیه تا جنسیت
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "علی تاریخچه")  # دو پیام کاربر + پرامپت‌های ربات لاگ شدند

    s = spy(reply_json={"value": "خانم"})
    # حالا سر جنسیت هستیم (تلفن رو بده اول)
    ss.handle_message(token, "09129876545")
    s2 = spy(reply_json={"value": "خانم"})
    ss.handle_message(token, "من خانم هستم")  # interpret_choice باید history داشته باشد

    all_msgs = json.dumps(s2.calls[0]["messages"], ensure_ascii=False)
    # پیام قبلی کاربر باید در context آمده باشد
    assert "علی تاریخچه" in all_msgs
    # و پرامپت‌های قبلی ربات هم (assistant)
    roles = [m.get("role") for m in s2.calls[0]["messages"]]
    assert "assistant" in roles or "علی تاریخچه" in all_msgs


def test_medium_match_outage_saves_lead_no_intro_count(dbs, medium_tenant, spy, monkeypatch):
    """متوسط + خطای LLM در مچینگ: پیام خطا، لید ذخیره، شمارنده معرفی صفر (Q22)"""
    _fake_profiles(monkeypatch)
    token = ss.create_session(1)
    spy(reply_text="بسیار خب، ادامه بدهیم.")  # LLM سالم برای کلِ پیاده‌روی تا GHQ
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "علی نهایی")
    ss.handle_message(token, "09129876545")
    ss.handle_message(token, "آقا")
    ss.handle_message(token, "30")
    ss.handle_message(token, "اضطراب شدید")
    ss.handle_message(token, "خیر")           # سابقه → رد شدن prev_detail → ترجیح جنسیت
    ss.handle_message(token, "فرقی ندارد")     # ترجیح جنسیت → شعبه (expectation حذف شد)
    reply = ss.handle_message(token, "اهمیتی ندارد")  # شعبه → پرسش قبولی GHQ
    assert reply is not None
    session = ss.get_session(token)
    assert session["state"] == -3
    spy(error=True)  # LLM قطع شد — فقط برای مچینگ پایانی
    reply = ss.handle_message(token, "خیر")    # رد کردن GHQ → _finish → مچینگ
    session = ss.get_session(token)
    assert session["state"] == -2
    # متوسط + خرابی LLM مچینگ → پیام خطا، نه معرفی جعلی
    assert llm_flow.OUTAGE_MSG in reply.text
    state = plan_service.get_tenant_plan_state(1)
    assert state["introductions_used"] == 0
    # لید ذخیره شده (users/request حتی بدون معرفی)
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    n = conn.execute("SELECT COUNT(*) FROM user_requests WHERE tenant_id=1").fetchone()[0]
    conn.close()
    assert n >= 1


def test_advanced_falls_back_to_rule_on_outage(dbs, spy, monkeypatch):
    """پیشرفته + خطای LLM مچینگ → fallback به engine (Q10 الف)"""
    from app.internal_ai_engine import engine
    _fake_profiles(monkeypatch)
    suffix = uuid.uuid4().hex[:8]
    pid = plan_service.create_plan(
        f"پیشرفته {suffix}", plan_service.MODE_ADVANCED,
        model="m", base_url="https://x/v1", api_key="k",
    )
    plan_service.assign_plan(1, pid, "tester")

    fake_recs = [{"name": "مریم احمدی", "reason": "تست", "score": 9.9,
                  "free_slots": 2, "gender": "خانم", "location": "ظفر"}]
    monkeypatch.setattr(engine, "match", lambda ui, tid=1: fake_recs)
    s = spy(error=True)

    token = ss.create_session(1)
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "فاطله")  # نام
    ss.handle_message(token, "09129876545")
    ss.handle_message(token, "خانم")
    ss.handle_message(token, "28")
    ss.handle_message(token, "خانواده")
    ss.handle_message(token, "خیر")
    ss.handle_message(token, "خانم")   # ترجیح جنسیت (expectation حذف شد)
    ss.handle_message(token, "شعبه مرکزی")  # شعبه (گزینه‌های فلوی نمونه)
    session = ss.get_session(token)
    if session["state"] == -3:
        reply = ss.handle_message(token, "خیر")
    else:
        reply = None
    assert reply is not None
    assert "مریم احمدی" in reply.text  # از engine آمد نه OUTAGE
    assert llm_flow.OUTAGE_MSG not in reply.text
    # معرفی نمایش داده شد → شمارش خورد
    state = plan_service.get_tenant_plan_state(1)
    assert state["introductions_used"] >= 1


# ── ۵ تلاش قبل از قطعی + دکمهٔ «تلاش مجدد» ──

def test_chat_completion_retries_up_to_five_attempts(dbs, monkeypatch):
    """خطای گذرا باید ۵ بار (اول + ۴ تکرار) تلاش شود، نه زود قطعی اعلام شود"""
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] < 5:
            raise llm_service.LLMError("پاسخ LLM بیش از 30 ثانیه طول کشید (timeout).")
        return "ok"

    monkeypatch.setattr(llm_service, "_post", flaky)
    out = llm_service.chat_completion("u", "k", "m", [], retries=4)
    assert out == "ok"
    assert calls["n"] == 5


def test_chat_completion_stops_after_five_failed_attempts(dbs, monkeypatch):
    """قطعِ واقعی بعد از ۵ تلاش بالاخره خطا می‌دهد — بی‌نهایت تکرار نمی‌شود"""
    calls = {"n": 0}

    def always_fail(*args, **kw):
        calls["n"] += 1
        raise llm_service.LLMError("timeout test")

    monkeypatch.setattr(llm_service, "_post", always_fail)
    with pytest.raises(llm_service.LLMError):
        llm_service.chat_completion("u", "k", "m", [], retries=4, retry_timeout=0.01)
    assert calls["n"] == 5


def test_chat_completion_does_not_retry_http_errors(dbs, monkeypatch):
    """خطای HTTP (کلید/مدل) تکرار نمی‌شود — همان یک بار کافی است"""
    calls = {"n": 0}

    def auth_fail(*args, **kw):
        calls["n"] += 1
        raise llm_service.LLMError("API key نامعتبر", status=401)

    monkeypatch.setattr(llm_service, "_post", auth_fail)
    with pytest.raises(llm_service.LLMError):
        llm_service.chat_completion("u", "k", "m", [], retries=4)
    assert calls["n"] == 1


def test_outage_reply_offers_retry_button(dbs, medium_tenant, spy):
    """پیام قطعی: متن «بعداً تلاش کنید» + دکمهٔ «تلاش مجدد» که سؤال جاری را
    دوباره می‌پرسد — بدون پیشرفت state و بدون مصرف پاسخ کاربر"""
    token = ss.create_session(1)
    spy(reply_text="بسیار خب.")
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "علی")
    ss.handle_message(token, "09129876545")
    spy(error=True)
    reply = ss.handle_message(token, "من خانم هستم")
    assert llm_flow.OUTAGE_MSG in reply.text
    assert "تلاش کنید" in reply.text          # بگوید بعداً تلاش کنید
    assert reply.quick_replies == ["تلاش مجدد"]
    assert reply.input_type == "chips+text"   # ورودی هم بماند برای ارسال دوباره

    # کلیک روی دکمه با LLM سالم: همان سؤال دوباره، state جلو نرفته
    spy(reply_text="لطفاً جنسیت خود را مشخص کنید.")
    reply2 = ss.handle_message(token, "تلاش مجدد")
    assert "جنسیت" in reply2.text
    session = ss.get_session(token)
    assert session["state"] == 4
    assert "gender" not in session["data"]

    # هنوز قطع است → باز هم همان پیام با دکمه
    spy(error=True)
    reply3 = ss.handle_message(token, "تلاش مجدد")
    assert llm_flow.OUTAGE_MSG in reply3.text
    assert reply3.quick_replies == ["تلاش مجدد"]


def test_opening_reply_always_includes_first_question(dbs, medium_tenant, spy):
    """خوش‌آمد هرگز بدون سؤال اول برنمی‌گردد — حتی وسط قطعی LLM (Q-خوش‌آمد)"""
    spy(error=True)  # LLM در لحظهٔ شروع/شروع مجدد قطع است
    cfg = llm_flow.get_tenant_llm(1)
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT flow_config FROM tenants WHERE id=1").fetchone()
    conn.close()
    flow = tenants_mod._parse_flow_config(row[0])

    reply = ss.opening_reply(flow, cfg)
    assert flow["welcome"] in reply.text     # خوش‌آمد هست
    assert "چه کسی" in reply.text            # و سؤال اول (رابطه) هم هست
    assert reply.quick_replies               # گزینه‌های سؤال اول آمده‌اند


def test_match_outage_retry_button_recovers(dbs, medium_tenant, spy, monkeypatch):
    """قطعی مچینگ: دکمهٔ «تلاش مجدد» → با برگشتن LLM معرفی می‌آید؛
    لید قبلاً ذخیره شده و دوباره ثبت نمی‌شود (شمارنده/ردیف دوبل نمی‌شود)"""
    _fake_profiles(monkeypatch)
    token = ss.create_session(1)
    spy(reply_text="بسیار خب، ادامه بدهیم.")
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "علی نهایی")
    ss.handle_message(token, "09129876545")
    ss.handle_message(token, "آقا")
    ss.handle_message(token, "30")
    ss.handle_message(token, "اضطراب شدید")
    ss.handle_message(token, "خیر")
    ss.handle_message(token, "فرقی ندارد")
    ss.handle_message(token, "اهمیتی ندارد")  # → پرسش قبولی GHQ
    spy(error=True)
    reply = ss.handle_message(token, "خیر")   # رد GHQ → مچینگ قطع
    assert llm_flow.OUTAGE_MSG in reply.text
    assert reply.quick_replies == ["تلاش مجدد"]
    session = ss.get_session(token)
    assert session["state"] == -2
    assert session["data"].get("_retry_match") == 1

    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    leads_before = conn.execute(
        "SELECT COUNT(*) FROM user_requests WHERE tenant_id=1"
    ).fetchone()[0]
    conn.close()
    assert leads_before >= 1  # لید در همان قطعی ثبت شده بود

    # LLM برگشت → «تلاش مجدد» → معرفی بدون ثبت دوبل
    spy(reply_json={"recommendations": [
        {"name": "مریم احمدی", "reason": "تطابق موضوع"},
    ]})
    reply2 = ss.handle_message(token, "تلاش مجدد")
    assert "مریم احمدی" in reply2.text
    assert llm_flow.OUTAGE_MSG not in reply2.text
    session = ss.get_session(token)
    assert session["state"] == -2
    assert not session["data"].get("_retry_match")  # فلگ پاک شد

    conn = get_conn(USER_RECORDS_DB)
    leads_after = conn.execute(
        "SELECT COUNT(*) FROM user_requests WHERE tenant_id=1"
    ).fetchone()[0]
    conn.close()
    assert leads_after == leads_before  # لید دوبل نشد
    state = plan_service.get_tenant_plan_state(1)
    assert state["introductions_used"] >= 1  # معرفی شمرده شد


def test_recommend_applies_hard_filter_before_llm(dbs, medium_tenant, spy, monkeypatch):
    """مسیر LLM باید از filter_profiles_for_user رد شود — وگرنه پلن پولی
    از weak سست‌تر می‌شد: فقط نام grounding می‌شد، نه شرایط صریح مراجع"""
    _fake_profiles(monkeypatch)
    from app.internal_ai_engine import filter_profiles_for_user as _real_filter

    calls = []

    def spy_filter(user_info, profiles):
        kept = _real_filter(user_info, profiles)
        calls.append((dict(user_info), list(profiles), kept))
        return kept

    monkeypatch.setattr(ss, "filter_profiles_for_user", spy_filter)

    token = ss.create_session(1)
    spy(reply_text="بسیار خب، ادامه بدهیم.")
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "علی نهایی")
    ss.handle_message(token, "09129876545")
    ss.handle_message(token, "آقا")
    ss.handle_message(token, "30")
    ss.handle_message(token, "اضطراب شدید")
    ss.handle_message(token, "خیر")
    ss.handle_message(token, "خانم")          # ترجیح جنسیت → فقط خانم‌ها
    ss.handle_message(token, "اهمیتی ندارد")  # شعبه → GHQ
    ss.handle_message(token, "خیر")           # رد GHQ → _finish → مچینگ

    assert calls, "filter_profiles_for_user در مسیر LLM صدا نخورد"
    ui, profs_in, profs = calls[-1]
    assert ui.get("preferred_gender") == "خانم"
    # ورودی خام هر دو مشاور است؛ خروجی فقط خانم‌ها — علی رضایی حذف شده
    assert [p["clean_name"] for p in profs_in] == ["مریم احمدی", "علی رضایی"]
    assert [p["clean_name"] for p in profs] == ["مریم احمدی"]
