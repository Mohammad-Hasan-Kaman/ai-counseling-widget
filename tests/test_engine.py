# -*- coding: utf-8 -*-
"""تست‌های طلایی موتور مچ — رفتار فعلی را قبل از refactore قفل می‌کنند"""
import uuid

import pytest

from app.internal_ai_engine import (
    engine, names_match, parse_age_bounds_perfect, get_consultant_gender,
    get_learning_weight, EXPANDED_SYNONYMS,
)


def _profiles():
    """پروفایل‌های کنترل‌شده — نام‌های مرد باید در MALE_COUNSELORS باشند
    (پیش‌فرض get_consultant_gender برای بقیه «خانم» است — رفتار مستندشده)"""
    raw = [
        {"name": "مریم احمدی", "ability": 3.0, "location": "هر دو",
         "education_experience": "روانشناس بالینی", "general_area": "اضطراب",
         "general_area_2": "", "specializations": {"اضطراب": "درمان اضطراب"},
         "detailed_topics": "اضطراب استرس پانیک", "age_range": "", "license": "", "notes": ""},
        {"name": "رضا محمدی", "ability": 2.0, "location": "ظفر",
         "education_experience": "", "general_area": "", "general_area_2": "",
         "specializations": {}, "detailed_topics": "نوجوان کنکور", "age_range": "۱۲-۲۰",
         "license": "", "notes": ""},
        {"name": "علی رضایی", "ability": 2.0, "location": "هر دو",
         "education_experience": "وکالت خانواده", "general_area": "حقوقی",
         "general_area_2": "", "specializations": {"مشاوره حقوقی خانواده": "حقوق"},
         "detailed_topics": "طلاق مهریه", "age_range": "", "license": "", "notes": ""},
        {"name": "زهرا کربلایی", "ability": 1.0, "location": "خیابان ایران",
         "education_experience": "روانپزشک اعصاب و روان", "general_area": "",
         "general_area_2": "", "specializations": {}, "detailed_topics": "روان پزشکان اعصاب و روان دارو",
         "age_range": "", "license": "", "notes": ""},
    ]
    return engine._index_profiles(raw)


@pytest.fixture()
def env(monkeypatch, dbs):
    """کنترل کامل ورودی‌های خارجی موتور.

    وزن‌های خودآموز فقط برای پلنِ یادگیرنده (حرفه‌ای/کامل) اعمال می‌شوند،
    پس اینجا یک مشتریِ حرفه‌ای ساخته می‌شود تا تزریقِ وزن واقعاً اثر کند."""
    from app import plan_service
    from app import tenants as tenants_mod
    t = tenants_mod.create_tenant(
        f"موتور {uuid.uuid4().hex[:6]}", f"eng_{uuid.uuid4().hex[:6]}", "EngPass123!")
    plan_service.assign_plan(t["tenant_id"], plan_service.create_plan(
        "حرفه‌ای", plan_service.MODE_ADVANCED,
        model="m", base_url="https://api.example.com/v1", api_key="sk-test",
        message_quota=1500, introduction_quota=80, created_by="tester"), "tester")

    state = {"profiles": _profiles(), "free": [], "weights": {},
             "tenant_id": t["tenant_id"]}
    monkeypatch.setattr(engine, "get_profiles", lambda tid, force=False: state["profiles"])
    monkeypatch.setattr("app.internal_ai_engine.load_free_appointments",
                        lambda tid=None: state["free"])
    monkeypatch.setattr("app.internal_ai_engine.load_learning_weights", lambda tid: state["weights"])
    return state


# ── نام و سن و جنسیت (ثابت‌ها) ──

def test_names_match_fuzzy():
    assert names_match("مهسا امیدبیکی", "مهساامیدبیگی")
    assert names_match("زهرا کربلایی", "خانم دکتر زهرا کربلائی")
    assert not names_match("زهرا مجاهدی", "زهرا نیلی")


def test_parse_age_bounds():
    assert parse_age_bounds_perfect("") == (0, 120)
    assert parse_age_bounds_perfect("۱۲-۱۹") == (12, 19)
    assert parse_age_bounds_perfect("بالای 18") == (18, 120)
    assert parse_age_bounds_perfect("زیر 12") == (0, 12)
    assert parse_age_bounds_perfect("تا پایان نوجوانی") == (0, 19)


def test_parse_age_bounds_elten_range():
    """«الی» باید به پارسر دامنه برسد — قبلاً «۱۸ الی ۲۵» می‌شد (18,120)
    و سن ۴۰ را برای مشاورِ مخصوصِ ۱۸-۲۵ مجاز می‌کرد"""
    assert parse_age_bounds_perfect("۱۸ الی ۲۵") == (18, 25)
    assert parse_age_bounds_perfect("18 الی 30") == (18, 30)
    # الگوی خرابِ بدون فاصله و «18 الی» بدون سقف: همچنان (18,120)
    assert parse_age_bounds_perfect("۱۸الی۰۰۰") == (18, 120)
    assert parse_age_bounds_perfect("18 الی") == (18, 120)


def test_gender_inference():
    assert get_consultant_gender("علی رضایی") == "آقا"
    assert get_consultant_gender("مریم احمدی") == "خانم"  # پیش‌فرض: خانم


# ── فیلترهای صلب و آبشاری ──

def test_gender_preference_hard_filter(env):
    recs = engine.match({
        "age": 30, "preferred_gender": "خانم", "branch": "اهمیتی ندارد",
        "topic": "اضطراب", "expectation": "",
    }, env["tenant_id"])
    assert recs
    assert all(r["gender"] == "خانم" for r in recs)


def test_age_filter_then_relax(env):
    # رضا محمدی ۱۲-۲۰؛ کاربر ۴۰ ساله → فیلتر سنی رد می‌کند ولی relax-age باید برگرداند
    # سید مجید به خاطر فیلتر حقوقی (موضوع غیرحقوقی) در هیچ سطحی برنمی‌گردد
    recs = engine.match({
        "age": 40, "preferred_gender": "آقا", "branch": "اهمیتی ندارد",
        "topic": "موضوع عمومی", "expectation": "",
    }, env["tenant_id"])
    names = [r["name"] for r in recs]
    assert "رضا محمدی" in names  # فقط با relax سنی برگشته
    assert all(r["gender"] == "آقا" for r in recs)  # جنسیت حفظ شده
    assert "مریم احمدی" not in names
    assert "علی رضایی" not in names  # حقوقی، غیرمجاز


def test_last_resort_keeps_gender_only(env):
    # فقط یک خانم با سن نامربوط + شعبه متفاوت — باید برگردد (relax کامل)
    env["profiles"] = engine._index_profiles([
        {"name": "فریبا نادری", "ability": 2.0, "location": "ظفر",
         "education_experience": "", "general_area": "", "general_area_2": "",
         "specializations": {}, "detailed_topics": "کودک", "age_range": "۰-۶",
         "license": "", "notes": ""},
    ])
    recs = engine.match({
        "age": 45, "preferred_gender": "خانم", "branch": "خیابان ایران",
        "topic": "موضوع عمومی", "expectation": "",
    }, env["tenant_id"])
    assert [r["name"] for r in recs] == ["فریبا نادری"]


def test_legal_exclusion_and_exception(env):
    # موضوع غیرحقوقی → مشاور حقوقی رد می‌شود
    recs = engine.match({
        "age": 30, "preferred_gender": "فرقی ندارد", "branch": "اهمیتی ندارد",
        "topic": "اضطراب", "expectation": "",
    }, env["tenant_id"])
    names = [r["name"] for r in recs]
    assert "علی رضایی" not in names
    # موضوع حقوقی → حقوقی مجاز است
    recs2 = engine.match({
        "age": 30, "preferred_gender": "فرقی ندارد", "branch": "اهمیتی ندارد",
        "topic": "طلاق و مهریه", "expectation": "",
    }, env["tenant_id"])
    names2 = [r["name"] for r in recs2]
    assert "علی رضایی" in names2


def test_branch_hard_filter(env):
    recs = engine.match({
        "age": 30, "preferred_gender": "فرقی ندارد", "branch": "ظفر",
        "topic": "نوجوان", "expectation": "",
    }, env["tenant_id"])
    # فقط مشاوران ظفر یا «هر دو»
    for r in recs:
        p = next(x for x in env["profiles"] if x["clean_name"] == r["name"])
        assert p["location"] in ("ظفر", "هر دو")


def test_custom_branch_activates_filter(env):
    """شعبهٔ تازه تعریف‌شدهٔ ادمین (مثلاً ونک) باید فیلتر را فعال کند"""
    from app.internal_ai_engine import _filter_candidates
    env["profiles"] = engine._index_profiles([
        {"name": "مونا ونکی", "ability": 2.0, "location": "ونک",
         "education_experience": "", "general_area": "", "general_area_2": "",
         "specializations": {}, "detailed_topics": "اضطراب", "age_range": "",
         "license": "", "notes": ""},
        {"name": "رضا محمدی", "ability": 2.0, "location": "ظفر",
         "education_experience": "", "general_area": "", "general_area_2": "",
         "specializations": {}, "detailed_topics": "نوجوان", "age_range": "",
         "license": "", "notes": ""},
        {"name": "مریم احمدی", "ability": 3.0, "location": "هر دو",
         "education_experience": "", "general_area": "", "general_area_2": "",
         "specializations": {}, "detailed_topics": "اضطراب", "age_range": "",
         "license": "", "notes": ""},
    ])
    cands = _filter_candidates(
        env["profiles"], gender_pref="فرقی ندارد", branch="ونک",
        user_age=30, is_legal_request=False,
    )
    locs = {p["location"] for p in cands}
    assert locs == {"ونک", "هر دو"}   # ظفر حذف شد — فیلتر واقعاً فعال است
    # بی‌تفاوت بودن همچنان بدون فیلتر (رفتار موجود)
    cands2 = _filter_candidates(
        env["profiles"], gender_pref="فرقی ندارد", branch="اهمیتی ندارد",
        user_age=30, is_legal_request=False,
    )
    assert {p["location"] for p in cands2} == {"ونک", "ظفر", "هر دو"}


def test_filter_profiles_for_user_hard_filters():
    """فیلتر صلبِ مسیر LLM — جنسیت/شعبه/حقوقی مثل weak باید رعایت شوند"""
    from app.internal_ai_engine import filter_profiles_for_user
    profiles = _profiles()

    # جنسیت: «خانم» → فقط خانم‌ها
    out = filter_profiles_for_user(
        {"preferred_gender": "خانم", "branch": "اهمیتی ندارد",
         "age": 0, "topic": "اضطراب", "expectation": ""},
        profiles)
    assert out and all(p["gender"] == "خانم" for p in out)

    # شعبه: «ظفر» → خیابان ایران حذف، «هر دو» و ظفر می‌مانند
    out2 = filter_profiles_for_user(
        {"preferred_gender": "فرقی ندارد", "branch": "ظفر",
         "age": 0, "topic": "اضطراب", "expectation": ""},
        profiles)
    locs = {p["location"] for p in out2}
    assert "خیابان ایران" not in locs
    assert "ظفر" in locs

    # موضوع غیرحقوقی → مشاورِ وکالت در کاندیداها نیست
    out3 = filter_profiles_for_user(
        {"preferred_gender": "فرقی ندارد", "branch": "اهمیتی ندارد",
         "age": 0, "topic": "اضطراب", "expectation": ""},
        profiles)
    names = {p["clean_name"] for p in out3}
    assert "علی رضایی" not in names
    assert "مریم احمدی" in names


def test_load_tenant_profiles_db_error_returns_empty(monkeypatch, dbs):
    """خطای DB نباید فهرستِ مستأجرِ دیگر را جایگزین کند (نشت بین مستأجرها)"""
    import app.internal_ai_engine as iae

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(iae.sqlite3, "connect", boom)
    assert engine._load_tenant_profiles(999999) == []


def test_count_free_for_alias_and_custom_branch():
    """شمارش نوبت آزاد: aliasهای قدیمی مثل قبل، شعبهٔ سفارشی دوطرفه، بی‌تفاوت همه"""
    from app.internal_ai_engine import _count_free_for_indexed
    idx = {"علی رضایی": ["zafar", "iran", "ونک", "روزولت"]}
    assert _count_free_for_indexed("علی رضایی", "ظفر", idx) == 1
    assert _count_free_for_indexed("علی رضایی", "زعفرانیه", idx) == 1
    assert _count_free_for_indexed("علی رضایی", "ایران", idx) == 1
    assert _count_free_for_indexed("علی رضایی", "خیابان ایران", idx) == 1
    assert _count_free_for_indexed("علی رضایی", "ونک", idx) == 1
    assert _count_free_for_indexed("علی رضایی", "اهمیتی ندارد", idx) == 4
    # شعبهٔ سفارشی که مشاوری آنجا نیست → صفر (فیلتر واقعی، نه شمارش کور همه)
    assert _count_free_for_indexed("علی رضایی", "جردن", idx) == 0
    # نام مشاور غلط → صفر
    assert _count_free_for_indexed("کس دیگری", "ظفر", idx) == 0


# ── امتیازدهی (golden) ──

def test_score_golden_concept_and_ability(env):
    """اضطراب: کلمه+12، قانون مفهوم+40، توانمندی ۳×۶=۱۸ → دقیقاً ۷۰"""
    recs = engine.match({
        "age": 30, "preferred_gender": "خانم", "branch": "اهمیتی ندارد",
        "topic": "اضطراب شدید", "expectation": "",
    }, env["tenant_id"])
    rec = next(r for r in recs if r["name"] == "مریم احمدی")
    assert rec["score"] == 70.0
    assert "اضطراب" in rec["reason"] or "استرس" in rec["reason"]


def test_score_free_slot_bonus_and_cap(env):
    env["free"] = [("مریم احمدی", "zafar"), ("مریم احمدی", "iran"), ("مریم احمدی", "zafar")]
    recs = engine.match({
        "age": 30, "preferred_gender": "خانم", "branch": "اهمیتی ندارد",
        "topic": "اضطراب شدید", "expectation": "",
    }, env["tenant_id"])
    rec = next(r for r in recs if r["name"] == "مریم احمدی")
    # 70 + min(3*3, 20) = 79
    assert rec["score"] == 79.0
    assert rec["free_slots"] == 3


def test_score_learning_weight_multiplier(env):
    env["weights"] = {("مریم احمدی", "اضطراب"): 1.3}
    recs = engine.match({
        "age": 30, "preferred_gender": "خانم", "branch": "اهمیتی ندارد",
        "topic": "اضطراب شدید", "expectation": "",
    }, env["tenant_id"])
    rec = next(r for r in recs if r["name"] == "مریم احمدی")
    assert rec["score"] == round(70.0 * 1.3, 2)


def test_learning_weights_ignored_on_non_learning_plan(env):
    """پلنِ پایه/استاندارد نباید وزنِ به‌جاماندهٔ دورهٔ حرفه‌ای را اعمال کند

    «یادگیری از بازخورد» فقط در حرفه‌ای/کامل فروخته می‌شود؛ بعد از تنزلِ
    مشتری، وزن‌های ذخیره‌شده نباید توصیه‌های موتور قاعده‌ای را جابه‌جا کنند."""
    from app import plan_service
    tid = env["tenant_id"]
    env["weights"] = {("مریم احمدی", "اضطراب"): 1.3}
    query = {"age": 30, "preferred_gender": "خانم", "branch": "اهمیتی ندارد",
             "topic": "اضطراب شدید", "expectation": ""}

    pro = next(r for r in engine.match(query, tid) if r["name"] == "مریم احمدی")
    assert pro["score"] == round(70.0 * 1.3, 2)   # روی حرفه‌ای اعمال می‌شود

    # تنزل به پایه: همان وزن‌ها نباید دیگر اثر کنند
    base_plan = plan_service.create_plan(
        "پایه", plan_service.MODE_WEAK,
        message_quota=-1, introduction_quota=30, created_by="tester")
    plan_service.assign_plan(tid, base_plan, "tester")

    after = next(r for r in engine.match(query, tid) if r["name"] == "مریم احمدی")
    assert after["score"] == 70.0, (
        f"وزنِ یادگیری روی پلنِ پایه اعمال شد ({after['score']}) — "
        "پلنی که آن قابلیت را نمی‌فروشد")


def test_ghq_high_risk_psychiatrist_boost(env):
    recs = engine.match({
        "age": 30, "preferred_gender": "فرقی ندارد", "branch": "اهمیتی ندارد",
        "topic": "حال عمومی بد", "expectation": "",
        "ghq_scores": {"total": 50, "depression": 16, "anxiety": 10, "somatic": 5, "social": 5},
    }, env["tenant_id"])
    rec = next(r for r in recs if r["name"] == "زهرا کربلایی")
    # توانمندی ۱×۶=۶ + روانپزشک high-risk +۶۰ = 66 (بدون کلمه/رایگان)
    assert rec["score"] == 66.0
    assert "GHQ" in rec["reason"] or "بالینی" in rec["reason"] or "روانپزشک" in rec["reason"]


def test_returns_top2_sorted(env):
    recs = engine.match({
        "age": 30, "preferred_gender": "فرقی ندارد", "branch": "اهمیتی ندارد",
        "topic": "اضطراب", "expectation": "",
    }, env["tenant_id"])
    assert len(recs) <= 2
    scores = [r["score"] for r in recs]
    assert scores == sorted(scores, reverse=True)


def test_two_reasons_never_identical(env):
    """«معرفی با دلیل» بی‌معنا بود اگر هر دو گزینه یک جمله بگیرند.

    قیمت‌گذاری «معرفی خودکار بهترین مشاور با دلیل» را برای همهٔ پلن‌ها تبلیغ می‌کند و
    مراجعِ دقیقاً همین سؤال را می‌پرسد: «پس فرق این دوتا چیست؟»."""
    recs = engine.match({
        "age": 30, "preferred_gender": "فرقی ندارد", "branch": "اهمیتی ندارد",
        "topic": "اضطراب", "expectation": "",
    }, env["tenant_id"])
    assert len(recs) == 2
    assert recs[0]["reason"] != recs[1]["reason"]


def test_reason_clauses_are_not_redundant(env):
    """دو تکهٔ دلیل نباید «عملاً یک حرف» باشند (مثال: «تخصص در روانشناسی کودک» /
    «تخصص اختصاصی در روانشناسی کودک»)"""
    from app.internal_ai_engine import _too_similar
    recs = engine.match({        "age": 30, "preferred_gender": "فرقی ندارد", "branch": "اهمیتی ندارد",
        "topic": "اضطراب", "expectation": "",
    }, env["tenant_id"])
    for rec in recs:
        # قالب دلیل «{چرا این؟}، {چه چیزیِ متفاوتی دارد؟}» است و خودِ «چرا» هم
        # ممکن است چند بند داشته باشد → از آخرین «، » جدا می‌شود
        why, diff = rec["reason"].rsplit("، ", 1)
        assert not _too_similar(why, diff)


def test_learning_weight_bounds():
    # فرمول واحد: 1 + log1p(pos)*0.12 - neg*0.10 در بازه [0.6, 1.6]
    import app.internal_ai_engine as eng
    from app.config import AI_KNOWLEDGE_DB
    import sqlite3
    import math
    conn = sqlite3.connect(str(AI_KNOWLEDGE_DB))
    try:
        conn.execute("DELETE FROM concept_feedback")
        conn.execute(
            "INSERT INTO concept_feedback (tenant_id, counselor_name, concept, positive_signals, negative_signals) "
            "VALUES (1, 'X', 'اضطراب', 200, 0)"
        )
        conn.execute(
            "INSERT INTO concept_feedback (tenant_id, counselor_name, concept, positive_signals, negative_signals) "
            "VALUES (1, 'Y', 'اضطراب', 0, 100)"
        )
        conn.execute(
            "INSERT INTO concept_feedback (tenant_id, counselor_name, concept, positive_signals, negative_signals) "
            "VALUES (1, 'Z', 'اضطراب', 5, 2)"
        )
        conn.commit()
        assert eng.get_learning_weight("X", "اضطراب", 1) == 1.6  # سقف (200 موفق → بالای 1.6)
        assert eng.get_learning_weight("Y", "اضطراب", 1) == 0.6  # کف
        # مقدار میانی دقیقاً طبق فرمول
        mid = 1.0 + math.log1p(5) * 0.12 - 2 * 0.10
        assert eng.get_learning_weight("Z", "اضطراب", 1) == max(0.6, min(1.6, mid))
        assert eng.get_learning_weight("بدون_سابقه", "اضطراب", 1) == 1.0
    finally:
        conn.execute("DELETE FROM concept_feedback")
        conn.commit()
        conn.close()


def test_concepts_dictionary_covers_clinical_set(env):
    # ۱۱ مفهوم بالینی طبق طراحی
    assert len(EXPANDED_SYNONYMS) == 11
    assert "اضطراب" in EXPANDED_SYNONYMS
    assert "حقوقی" in EXPANDED_SYNONYMS
