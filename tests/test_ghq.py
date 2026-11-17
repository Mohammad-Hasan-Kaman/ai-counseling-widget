# -*- coding: utf-8 -*-
"""تست‌های طلایی GHQ-28: امتیازدهی، تفسیر و آستانه‌های بالینی"""
from app.ghq_analyzer import (
    GHQ_QUESTIONS, SUBSCALES,
    calculate_ghq_scores, interpret_score, get_ghq_message,
)
from app.session_store import _parse_ghq_choice


def test_28_questions_and_subscales():
    assert len(GHQ_QUESTIONS) == 28
    all_idx = sorted(i for idxs in SUBSCALES.values() for i in idxs)
    assert all_idx == list(range(28))


def test_scoring_zero_and_max():
    assert calculate_ghq_scores([0] * 28) == {
        "somatic": 0, "anxiety": 0, "social": 0, "depression": 0, "total": 0,
    }
    scores = calculate_ghq_scores([3] * 28)
    assert scores["total"] == 84
    assert scores["depression"] == 21
    for key in ("somatic", "anxiety", "social"):
        assert scores[key] == 21


def test_interpret_score_boundaries():
    assert interpret_score(0) == "سالم"
    assert interpret_score(9) == "سالم"
    assert interpret_score(10) == "متوسط"
    assert interpret_score(15) == "متوسط"
    assert interpret_score(16) == "شدید"


def test_message_low_risk():
    msg = get_ghq_message(calculate_ghq_scores([0] * 28))
    assert "مطلوب" in msg
    assert "توجه تخصصی" not in msg


def test_message_medium_risk_total_24():
    # ۲۴ نمره کل — دقیقاً روی آستانهٔ «در معرض آسیب»، بدون عبور از آستانه شدید
    answers = [0] * 28
    answers[0:8] = [1] * 8
    answers[8:15] = [1] * 7
    answers[15:21] = [1] * 6
    answers[21:24] = [1] * 3  # total = 24
    scores = calculate_ghq_scores(answers)
    assert scores["total"] == 24
    assert scores["total"] >= 24
    assert scores["total"] < 43
    assert scores["depression"] < 15
    msg = get_ghq_message(scores)
    assert "در معرض آسیب" in msg
    assert "توجه تخصصی" not in msg


def test_message_high_risk_total_ge_43():
    answers = [2] * 28  # total = 56
    scores = calculate_ghq_scores(answers)
    assert scores["total"] >= 43
    msg = get_ghq_message(scores)
    assert "توجه تخصصی" in msg


def test_message_high_risk_depression_ge_15():
    # total زیر ۴۳ ولی افسردگی ≥ ۱۵
    answers = [0] * 28
    answers[21:28] = [3] * 5 + [0, 0]  # depression = 15
    scores = calculate_ghq_scores(answers)
    assert scores["total"] == 15
    assert scores["depression"] == 15
    assert scores["total"] < 43
    msg = get_ghq_message(scores)
    assert "توجه تخصصی" in msg


# ── انتخاب گزینه از متن آزاد ──

def test_parse_ghq_choice_longest_match_wins():
    """«بسیار بدتر از معمول» نباید به‌خاطر زیررشته بودن گزینهٔ ملایم‌تر شود"""
    opts = GHQ_QUESTIONS[0][1]  # بیش از معمول / در حد معمول / بدتر از معمول / بسیار بدتر از معمول
    assert _parse_ghq_choice("بسیار بدتر از معمول", opts) == 3  # قبلاً ۲ برمی‌گشت
    assert _parse_ghq_choice("بدتر از معمول", opts) == 2
    assert _parse_ghq_choice("در حد", opts) == 1                 # کوتاه‌ترین گزینهٔ حاوی


def test_parse_ghq_choice_chip_format():
    """چیپس «۱ - در حد معمول» — پیشوند عددی، ارقام فارسی بعد از ترجمه"""
    opts = GHQ_QUESTIONS[0][1]
    assert _parse_ghq_choice("۱ - در حد معمول", opts) == 1
    assert _parse_ghq_choice("3 - بسیار بدتر از معمول", opts) == 3


def test_parse_ghq_choice_exact_beats_container():
    opts = GHQ_QUESTIONS[1][1]  # به هیچ وجه / نه بیش از معمول / بیش از معمول / بسیار بیشتر از معمول
    assert _parse_ghq_choice("بیش از معمول", opts) == 2  # نه «نه بیش از معمول» (۱)
    assert _parse_ghq_choice("نه بیش از معمول", opts) == 1
    assert _parse_ghq_choice("هیچی", opts) is None
