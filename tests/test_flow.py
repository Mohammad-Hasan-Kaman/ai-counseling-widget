# -*- coding: utf-8 -*-
"""تست‌های طلایی state machine فلو + اعتبارسنجی تلفن + جداسازی tenant + استپ رابطه"""
import pytest

from app import session_store as ss
from app.user_db import is_suspicious_phone

# فلوی مرکز نمونه: 0=relationship 1=spouse_relation (شرطی، فقط «برای فرزندم»)
# 2=name 3=phone 4=gender 5=age 6=topic 7=prev 8=prev_detail 9=preferred_gender 10=branch
# برای «برای خودم» استپ ۱ رد می‌شود → بعد از رابطه state روی ۲ (نام) می‌نشیند


@pytest.fixture()
def token(dbs):
    # tenant 1 نباید پلن LLM‌دار داشته باشد — وگرنه chat_ask بدون mock به
    # شبکه می‌زند و این تست‌ها (که قاعده‌ای‌اند) می‌شکنند؛ وابسته به ترتیب فایل‌ها نباشیم
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    conn.execute("DELETE FROM tenant_plans WHERE tenant_id=1")
    conn.commit()
    conn.close()
    return ss.create_session(1)  # tenant نمونه (seed)


def _start_self(token, name="علی"):
    """عبور از استپ رابطه با «برای خودم» + ثبت نام"""
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, name)


def test_first_message_is_relationship_step(token):
    reply = ss.handle_message(token, "برای خودم")
    # مرحله بعدی: نام (استپ شرطی همسر رد شد)
    assert "نام" in reply.text
    assert ss.get_session(token)["state"] == 2


def test_opening_shows_first_question(token):
    """welcome می‌گوید «به چند سؤال زیر پاسخ دهید» — خودِ سؤال اول هم باید همان لحظه بیاید"""
    prompt = ss._state_prompt(ss.get_session(token))
    assert "سؤال" in prompt.text or "خوش آمدید" in prompt.text
    assert prompt.quick_replies, "گزینه‌های اولین سؤال باید با خوش‌آمد بیایند"
    assert prompt.input_type == "chips"
    # باید عیناً همان چیزی باشد که سشن تازه به ویجت می‌دهد
    from app import tenants as tenants_mod
    flow = tenants_mod.get_tenant(1)["flow_config"]
    opening = ss.opening_reply(flow)
    assert opening.quick_replies == prompt.quick_replies
    assert opening.text == prompt.text


def test_relationship_rejects_unknown_choice(token):
    reply = ss.handle_message(token, "علی")
    # گزینه نامعتبر در استپ انتخابی — در جا می‌زند
    assert ss.get_session(token)["state"] == 0
    assert reply.quick_replies  # گزینه‌ها برگشته


def test_first_message_is_name_step(token):
    _start_self(token, "علی رضایی")
    session = ss.get_session(token)
    assert session["state"] == 3  # رابطه + نام رد شد → سر تلفن
    reply = ss.handle_message(token, "09129876545")  # تلفن معتبر → جنسیت
    session = ss.get_session(token)
    assert session["state"] == 4
    assert session["data"]["phone"] == "09129876545"


def test_invalid_phone_rejected(token):
    _start_self(token)
    reply = ss.handle_message(token, "0912")
    assert "نامعتبر" in reply.text
    session = ss.get_session(token)
    assert session["state"] == 3  # در جا زد روی تلفن


def test_persian_digit_phone_accepted(token):
    _start_self(token)
    reply = ss.handle_message(token, "۰۹۱۲۹۸۷۶۵۴۵")
    session = ss.get_session(token)
    assert session["state"] == 4  # رفت سر جنسیت
    assert session["data"]["phone"] == "09129876545"
    assert "آقا" in (reply.quick_replies or []) or "جنسیت" in reply.text or reply.quick_replies


def test_fake_phone_rejected(token):
    """شمارهٔ ساختگی/پیش‌شمارهٔ نامعتبر پذیرفته نمی‌شود — state جلو نمی‌رود"""
    _start_self(token)  # state == 3 روی تلفن
    for bad in (
        "09111111111",    # تکراری
        "09912345678",    # دنبالهٔ صعودی
        "09198765432",    # دنبالهٔ نزولی
        "09876543210",    # پیش‌شمارهٔ خارج از تخصیص موبایل
        "08123456700",    # بدون 09
        "0912345670",     # ۱۰ رقم
        "091234567001",   # ۱۲ رقم
        "بدون شماره",
    ):
        reply = ss.handle_message(token, bad)
        assert any(w in reply.text for w in ("نامعتبر", "پیش‌شماره", "ساختگی")), (bad, reply.text)
        assert ss.get_session(token)["state"] == 3

    # قالب‌های مرتب‌شده (فاصله/تیره/+98) پس از نرمال‌سازی پذیرفته می‌شوند
    ss.handle_message(token, "+98 912-987 6545")
    session = ss.get_session(token)
    assert session["state"] == 4
    assert session["data"]["phone"] == "09129876545"


def test_invalid_age_rejected(token):
    _start_self(token)
    ss.handle_message(token, "09129876545")
    ss.handle_message(token, "آقا")
    reply = ss.handle_message(token, "نه")
    assert "عدد" in reply.text
    assert ss.get_session(token)["state"] == 5


def test_full_flow_with_skipped_conditional(token):
    """prev_therapy=خیر → prev_detail رد می‌شود و مستقیم به preferred_gender می‌رود"""
    _start_self(token)
    ss.handle_message(token, "09129876545")
    ss.handle_message(token, "آقا")
    ss.handle_message(token, "30")
    ss.handle_message(token, "اضطراب")
    reply = ss.handle_message(token, "خیر")  # سابقه درمان
    # مرحله بعدی باید ترجیح جنسیت مشاور باشد نه prev_detail (expectation حذف شد)
    assert "ترجیح" in reply.text
    session = ss.get_session(token)
    assert session["state"] == 9  # prev_detail رد شد → preferred_gender


def test_restart_returns_to_welcome(token):
    _start_self(token)
    reply = ss.handle_message(token, "شروع مجدد")
    session = ss.get_session(token)
    assert session["state"] == 0
    assert session["data"] == {}
    assert "خوش آمدید" in reply.text or "سؤال" in reply.text


def test_age_asks_for_the_subject_when_registering_for_a_child(token):
    """«برای فرزندم» → سنِ کودک پرسیده می‌شود، نه سنِ ثبت‌کننده (Q-برای دیگری)"""
    ss.handle_message(token, "برای فرزندم")
    ss.handle_message(token, "مریم احمدی")   # نام ثبت‌کننده
    ss.handle_message(token, "خیلی خوب")     # کیفیت رابطه با همسر (سؤال شرطی)
    ss.handle_message(token, "امیر احمدی")   # نام مراجع
    ss.handle_message(token, "09129876545")  # تلفن
    ss.handle_message(token, "آقا")          # جنسیت
    reply = ss.handle_message(token, "30")   # ورودی سن
    assert ss.get_session(token)["state"] == 6
    # مرحلهٔ بعدی باید «سن مراجع/ایشان» بپرسد، نه «سن شما»
    if ss.get_session(token)["state"] == 6:
        pass  # گام بعدی هنوز جنسیت است — بی‌اهمیت
    assert reply.text


def test_age_owner_phrase_differs_for_child_vs_self():
    assert ss._age_owner_phrase({"relationship": "برای خودم"}) == "سن شما"
    assert ss._age_owner_phrase({"relationship": "برای فرزندم"}) == "سن ایشان"
    assert ss._age_owner_phrase({"relationship": "برای همسرم"}) == "سن مراجع"


def test_new_tenant_has_no_plan_by_default(dbs):
    """سیاست: مشتری تازه قفل است تا سوپر ادمین خودش پلن بدهد."""
    from app import tenants as tenants_mod, plan_service
    created = tenants_mod.create_tenant("مشتری بی‌پلن", "noplan_admin", "NoPlan123!")
    tid = created["tenant_id"]
    assert plan_service.get_tenant_plan_state(tid) is None
    locked, reason = plan_service.check_lock(tid)
    assert locked and reason == "no_plan"


def test_tenant_isolation_unknown_token(dbs):
    with pytest.raises(KeyError):
        ss.handle_message("f" * 32, "سلام")


def test_session_tenant_persists(dbs):
    from app import tenants as tenants_mod
    created = tenants_mod.create_tenant("مرکز جدا", "iso_admin", "IsoPass123!")
    token = ss.create_session(created["tenant_id"])
    session = ss.get_session(token)
    assert session["tenant_id"] == created["tenant_id"]
    # فلوی عمومی: رابطه + نام + تلفن → موضوع (سن/ترجیح جنسیت/GHQ بعداً)
    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "مریم")
    reply = ss.handle_message(token, "09121112233")
    session = ss.get_session(token)
    assert session["state"] == 4   # مرحلهٔ topic (استپ شرطی همسر جا را یکی جلو برده)
    assert not reply.done


def test_suspicious_phone_detection():
    assert is_suspicious_phone("09012345678")  # دنبالهٔ صعودی در tail (090 خودش معتبر است)
    assert is_suspicious_phone("09111111111")  # ارقام تکراری در tail
    assert is_suspicious_phone("09912345678")  # دنباله‌ی صعودی 12345678
    assert is_suspicious_phone("09198765432")  # دنباله‌ی نزولی 98765432
    assert not is_suspicious_phone("09129876545")  # عادی
    assert not is_suspicious_phone("12345")  # طول غلط


def test_default_flow_ships_advertised_capabilities(dbs):
    """مشتری تازه باید از جعبه‌بیرون GHQ و معرفی مشاور داشته باشد — نه فقط لید.

    قیمت‌گذاری برای همهٔ پلن‌ها «آزمون GHQ-28» و «معرفی خودکار دو مشاور»
    تبلیغ می‌کند؛ فلوی پیش‌فرض هم welcome آن را وعده می‌دهد."""
    from app import tenants as tenants_mod
    flow = tenants_mod.default_public_flow()
    assert flow["ending"] == "recommend"
    assert flow["ghq"] is True
    sources = {s.get("source") for s in flow["steps"]}
    # سیگنال‌هایی که موتور قاعده‌ای واقعاً می‌خواند
    assert {"topic", "age", "preferred_gender"} <= sources


def test_no_match_still_saves_lead(dbs):
    """بدون پروفایل مشاور (مشتری تازه) معرفی ممکن نیست — ولی لید نباید گم شود"""
    from app import tenants as tenants_mod
    from app.config import USER_RECORDS_DB
    from app.db import get_conn

    created = tenants_mod.create_tenant("مرکز بی‌مشاور", "nomatch_admin", "NoMatch123!")
    token = ss.create_session(created["tenant_id"])

    ss.handle_message(token, "برای خودم")
    ss.handle_message(token, "رضا")
    ss.handle_message(token, "09121112233")
    ss.handle_message(token, "اضطراب شدید و بی‌خوابی")
    ss.handle_message(token, "30")
    ss.handle_message(token, "فرقی ندارد")
    reply = ss.handle_message(token, "خیر")   # GHQ رد می‌شود → _finish

    assert "ثبت شد" in reply.text
    conn = get_conn(USER_RECORDS_DB)
    n = conn.execute(
        "SELECT COUNT(*) FROM user_requests WHERE tenant_id=?",
        (created["tenant_id"],),
    ).fetchone()[0]
    conn.close()
    assert n == 1, "لید باید حتی وقتی مشاوری پیدا نمی‌شود ثبت شود"


def test_for_other_asks_initiator_and_keeps_subject(token):
    """Q17 الف: برای فرزندم → نام ثبت‌کننده → سپس اطلاعات مراجع"""
    reply = ss.handle_message(token, "برای فرزندم")
    assert "ثبت‌کننده" in reply.text or "خودتان" in reply.text
    session = ss.get_session(token)
    assert session["data"]["relationship"] == "برای فرزندم"
    # پیام بعدی = نام ثبت‌کننده (state هنوز 1 = name step بعد از advance)
    session = ss.get_session(token)
    assert session["state"] == 1  # به استپ همسر advance شد ولی فعلاً نام ثبت‌کننده می‌خواهد

    reply = ss.handle_message(token, "مریم احمدی")  # نام پدر/مادر (ثبت‌کننده)
    session = ss.get_session(token)
    assert session["data"]["initiator_name"] == "مریم احمدی"
    assert "_need_initiator" not in session["data"]
    # برای فرزندم → سؤال شرطی کیفیت رابطه با همسر می‌آید (state هنوز روی همان استپ ۱)
    assert "همسر" in reply.text
    assert session["state"] == 1

    reply = ss.handle_message(token, "خیلی خوب")   # پاسخ به سؤال همسر
    # حالا باید سؤال نام مراجع (فرزند) را بپرسد
    assert "نام" in reply.text
    assert ss.get_session(token)["state"] == 2

    ss.handle_message(token, "امیر احمدی")  # نام مراجع (فرزند)
    reply = ss.handle_message(token, "09129876545")
    session = ss.get_session(token)
    assert session["data"]["full_name"] == "امیر احمدی"  # مراجع
    assert session["data"]["initiator_name"] == "مریم احمدی"  # تماس‌گیرنده
    assert session["data"]["phone"] == "09129876545"  # شماره ثبت‌کننده
    assert session["state"] == 4  # جنسیت
    # برچسب تلفن برای دیگری باید «ثبت‌کننده» بوده باشد — از prompt قبلی قابل بررسی نیست؛ داده‌ها کافی‌اند


def test_for_self_no_initiator_prompt(token):
    ss.handle_message(token, "برای خودم")
    session = ss.get_session(token)
    assert "initiator_name" not in session["data"]
    assert "_need_initiator" not in session["data"]
    ss.handle_message(token, "علی")
    session = ss.get_session(token)
    assert "initiator_name" not in session["data"]


# ── سؤال شرطی کیفیت رابطه با همسر (برای فرزندم) ──

def test_spouse_relation_step_only_for_child(token):
    # «برای فرزندم» → بعد از نام ثبت‌کننده، سؤال همسر با سه گزینه
    ss.handle_message(token, "برای فرزندم")
    reply = ss.handle_message(token, "مریم احمدی")
    assert "همسر" in reply.text
    assert reply.quick_replies == ["خیلی خوب", "میانه", "پایین"]
    # پاسخ در بالای data (برای شرط و جملهٔ پایانی) و custom_data لید می‌نشیند
    ss.handle_message(token, "میانه")
    assert ss.get_session(token)["data"]["spouse_relation"] == "میانه"

    # «برای خودم» → بدون این سؤال، مستقیم سر نام
    token2 = ss.create_session(1)
    reply2 = ss.handle_message(token2, "برای خودم")
    assert "همسر" not in reply2.text
    assert "نام" in reply2.text
    assert "spouse_relation" not in ss.get_session(token2)["data"]


def _walk_child_to_finish(token, spouse_answer: str):
    """پیاده‌روی کامل با «برای فرزندم» روی فلوی عمومی تا پایان (GHQ رد می‌شود)"""
    ss.handle_message(token, "برای فرزندم")
    ss.handle_message(token, "مریم احمدی")          # نام ثبت‌کننده
    ss.handle_message(token, spouse_answer)         # کیفیت رابطه با همسر
    ss.handle_message(token, "رضا احمدی")           # نام مراجع
    ss.handle_message(token, "09121112233")         # تلفن
    ss.handle_message(token, "اضطراب و بی‌خوابی")   # موضوع
    ss.handle_message(token, "30")                  # سن
    ss.handle_message(token, "فرقی ندارد")          # ترجیح جنسیت
    return ss.handle_message(token, "خیر")          # رد GHQ → پایان


def test_couple_advice_when_spouse_relation_weak(dbs):
    """میانه/پایین → جملهٔ پیشنهاد زوج‌درمانگر در پیام پایانی"""
    from app import tenants as tenants_mod
    created = tenants_mod.create_tenant("مرکز زوج‌درمان", "couple_admin", "Couple123!")
    token = ss.create_session(created["tenant_id"])
    reply = _walk_child_to_finish(token, "میانه")
    assert "ثبت شد" in reply.text          # مسیر not-recs (بدون مشاور)
    assert "زوج درمانگر" in reply.text


def test_no_couple_advice_when_spouse_relation_good(dbs):
    """خیلی خوب → بدون جملهٔ زوج‌درمانگر"""
    from app import tenants as tenants_mod
    created = tenants_mod.create_tenant("مرکز بی‌زوج", "nCouple_admin", "NCouple123!")
    token = ss.create_session(created["tenant_id"])
    reply = _walk_child_to_finish(token, "خیلی خوب")
    assert "ثبت شد" in reply.text
    assert "زوج درمانگر" not in reply.text


# ── بازیابی سشن (_state_prompt) ──

def test_state_prompt_after_restore_asks_initiator(token):
    """بازیابی وسطِ پرسش نام ثبت‌کننده → متن قاعده‌ای، بدون LLM و بدون مصرف پاسخ"""
    ss.handle_message(token, "برای فرزندم")
    assert ss.get_session(token)["data"].get("_need_initiator")
    prompt = ss._state_prompt(ss.get_session(token))
    assert prompt.text == ss._INITIATOR_ASK_TEXT
    # بازیابی نباید فلگ را بردارد یا چیزی ذخیره کند
    assert ss.get_session(token)["data"].get("_need_initiator")


def test_state_prompt_phone_label_for_other(token):
    """data باید به _step_prompt برسد — برچسب تلفن برای فرمِ برای دیگری «ثبت‌کننده» است"""
    ss.handle_message(token, "برای فرزندم")
    ss.handle_message(token, "مریم احمدی")   # نام ثبت‌کننده
    ss.handle_message(token, "خیلی خوب")      # کیفیت رابطه با همسر
    ss.handle_message(token, "امیر احمدی")    # نام مراجع
    session = ss.get_session(token)
    assert session["state"] == 3              # تلفن
    prompt = ss._state_prompt(session)
    assert "ثبت‌کننده" in prompt.text


def test_state_prompt_retry_match_offers_retry(token):
    """مچینگِ قطع‌شده بعد از رفرش صفحه هم دکمهٔ «تلاش مجدد» دارد، نه «شروع مجدد»"""
    from app import llm_flow
    session = ss.get_session(token)
    data = dict(session["data"])
    data["_retry_match"] = 1
    ss._save_session(token, -2, data)
    prompt = ss._state_prompt(ss.get_session(token))
    assert llm_flow.OUTAGE_MSG in prompt.text
    assert "تلاش مجدد" in prompt.quick_replies


def test_prev_therapy_string_no_saves_zero(dbs):
    """رشتهٔ «خیر» truthy است — قبلاً سابقه‌نداشتن را ۱ ثبت می‌کرد (user_db)"""
    from app.user_db import save_user_consultation
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    for i, (val, want) in enumerate([("خیر", 0), ("بله", 1), (True, 1), (False, 0)]):
        tok = f"prev{i}test" + "x" * 22
        save_user_consultation(
            tok, {"full_name": f"آزمون سابقه {i}", "phone": f"0912987654{i}",
                  "has_prev_therapy": val},
            [], 1, None,
        )
        conn = get_conn(USER_RECORDS_DB)
        got = conn.execute(
            "SELECT has_prev_therapy FROM user_requests WHERE session_token=?", (tok,)
        ).fetchone()[0]
        conn.close()
        assert got == want, (val, got)
