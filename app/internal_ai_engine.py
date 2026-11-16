# -*- coding: utf-8 -*-
"""
Internal Spiral AI Engine & Clinical Decision Matcher
موتور تریاژ بالینی per-tenant — بازنویسی‌شده: ثابت‌های نام‌دار، فرمول واحد
یادگیری، فیلتر آبشاری تک‌تابعی و pipeline امتیازدهی (رفتار = تست‌های طلایی).

نکته‌ی داده‌ای (مستندشده): get_consultant_gender فقط ۲۲ نام مرد را می‌شناسد و
بقیه را «خانم» برمی‌گرداند — تغییر این قاعده نیازمند داده‌ی جنسیت ساختاریافته است.
"""

import re
import json
import sqlite3
import math
import time
import threading
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple

from app.config import PROFILES_JSON, APPOINTMENTS_DB, AI_KNOWLEDGE_DB, USER_RECORDS_DB
from app.ghq_analyzer import GHQ_SEVERE_TOTAL, GHQ_SEVERE_DEPRESSION

logger = logging.getLogger(__name__)
LEARNING_DB = AI_KNOWLEDGE_DB

# ── ثابت‌های domain ──

# Example names only — deployments must replace this set with the male
# counselors of their own roster (used for name-based gender matching when
# the roster data carries no gender field).
MALE_COUNSELORS = {
    "علی رضایی", "رضا محمدی", "حسن کریمی", "محمد احمدی", "امیر حسینی",
    "مهدی موسوی", "حسین صادقی", "سعید نجفی", "جواد عزیزی", "کاظم رحیمی",
}

# فرمول یادگیری (تنها منبع — قبلاً دو بار کپی شده بود)
LEARN_POS_COEF = 0.12
LEARN_NEG_COEF = 0.10
LEARN_WEIGHT_MIN = 0.6
LEARN_WEIGHT_MAX = 1.6

# امتیازدهی
SCORE_KEYWORD_HIT = 12.0
SCORE_ABILITY_MULTIPLIER = 6.0
SCORE_FREE_SLOT_POINT = 3.0
SCORE_FREE_SLOT_CAP = 20.0
SCORE_GHQ_PSYCHIATRIST = 60.0
SCORE_GHQ_CLINICAL = 35.0
SCORE_GHQ_SUBSCALE_PER_POINT = 1.5
SCORE_GHQ_SUBSCALE_THRESHOLD = 12
TOP_N_RESULTS = 2
AGE_SLACK = 1  # تساهل ±۱ سال در فیلتر سنی صلب

# کش پروفایل: TTL ثانیه — بین چند worker با upload ناسازگار نماند (force همیشه فوری)
PROFILE_CACHE_TTL = 60.0

NUM_WORDS = {
    "صفر": "0", "یک": "1", "دو": "2", "سه": "3", "چهار": "4", "پنج": "5",
    "شش": "6", "هفت": "7", "هشت": "8", "نه": "9", "ده": "10",
    "یازده": "11", "دوازده": "12", "سیزده": "13", "چهارده": "14", "پانزده": "15",
    "شانزده": "16", "هفده": "17", "هجده": "18", "نوزده": "19", "بیست": "20"
}

EXPANDED_SYNONYMS = {
    "والد_فرزند": [
        "پدر", "مادر", "بابا", "مامان", "والد", "والدین", "فرزند", "پسر", "دختر",
        "تعارض با والدین", "دعوا با پدر", "دعوا با مادر", "خانوادگی", "روابط والد",
        "فرزندپروری", "والدگری", "روابط والد _ فرزند", "بهبود روابط والد", "تربیت فرزند",
        "تعارض با خانواده", "مشکل با پدر", "مشکل با مادر", "خانواده", "تربیت", "خانواده‌درمانی"
    ],
    "ارتباط_تعارض": [
        "اختلاف", "حرف", "حرف زدن", "گفتگو", "صحبت", "مکالمه", "روابط بین فردی",
        "بین‌فردی", "بین فردی", "مهارت‌های ارتباطی", "تعارض", "تعارضات", "ارتباطی",
        "درست حرف", "مهارت های زندگی", "مهارت‌های زندگی", "ارتباط با دیگران",
        "حل اختلاف", "درک متقابل", "ناسازگاری", "کل‌کل", "جروبحث", "دعوا", "روابط"
    ],
    "اضطراب": [
        "استرس", "دلشوره", "نگرانی", "تپش قلب", "پانیک", "ترس", "فوبیا", "anxiety",
        "وحشت", "بی‌قراری", "اضطرابی", "اضطراب", "فوبیاها", "بیقراری"
    ],
    "افسردگی": [
        "غم", "بی‌حوصلگی", "پوچی", "ناامیدی", "افسرده", "انگیزه", "depression",
        "خلق پایین", "گریه", "بی‌انرژی", "خلق", "خلقی", "افسردگی", "بی انگیزه", "انگیزشی", "سوگ"
    ],
    "وسواس": [
        "وسواسی", "شستشو", "فکر تکراری", "چک کردن", "ocd", "نشخوار فکری", "افکار مزاحم", "وسواس"
    ],
    "زوج_ازدواج": [
        "همسر", "ازدواج", "طلاق", "خیانت", "تعارض زناشویی", "دعوا با همسر",
        "روابط زوجین", "پیش از ازدواج", "نامزدی", "سکس", "جنسی", "سکستراپی", "زناشویی", "زوج", "واژینیسموس"
    ],
    "کودک": [
        "بچه", "کودک", "خردسال", "پرخاشگری کودک", "بیش‌فعالی", "adhd", "لجبازی",
        "شب ادراری", "تغذیه کودک", "خواب کودک", "مهد", "اوتیسم", "ناخن جویدن", "دلبستگی", "کودکان", "پوشک", "یادگیری"
    ],
    "نوجوان_جوان": [
        "بلوغ", "افت تحصیلی", "کنکور", "انتخاب رشته", "هویت", "لجبازی نوجوان",
        "ارتباط با جنس مخالف", "دوست‌یابی", "نوجوانی", "نوجوان", "جوان", "۱۸ سال", "18 سال", "کنکوری"
    ],
    "توسعه_فردی": [
        "عزت نفس", "اعتماد به نفس", "کوچینگ", "هدف‌گذاری", "رشد فردی", "مهارت‌های زندگی",
        "سبک زندگی", "خودشناسی", "کمال گرایی", "اهمال کاری", "توسعه فردی", "کمال‌گرایی", "اهمال‌کاری"
    ],
    "حقوقی": [
        "مهریه", "حضانت", "نفقه", "دادگاه", "وکیل", "طلاق قانونی", "ارث", "محجور", "وکالت", "قضائیه", "حقوقی"
    ],
    "پزشکی_روانپزشکی": [
        "دارو", "دارودرمانی", "روانپزشک", "اعصاب و روان", "سایکوتیک", "بیماری جسمی", "طب ایرانی", "دوقطبی"
    ]
}


def get_consultant_gender(name: str) -> str:
    name_clean = name.strip()
    for m in MALE_COUNSELORS:
        if m in name_clean or name_clean in m:
            return "آقا"
    return "خانم"  # پیش‌فرض داده‌ای — تغییر نیازمند ستون جنسیت ساختاریافته


# نرمال‌سازی نام برای تطابق پروفایل‌های اکسل با رکوردهای سایت نوبت‌دهی
_TITLE_PATTERNS = [r"\bآقای\b", r"\bخانم\b", r"\bدکتر\b", r"\bدكتر\b", r"^اپراتور\s*\d*"]


def normalize_counselor_name(name: str) -> str:
    s = " ".join(str(name).split())
    for pat in _TITLE_PATTERNS:
        s = re.sub(pat, "", s)
    s = " ".join(s.split())
    s = s.replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def names_match(a: str, b: str) -> bool:
    """تطابق فازی دو نام مشاور: نرمال + حذف فاصله‌ها + تطابق جزئی/فاصله‌ای"""
    na, nb = normalize_counselor_name(a), normalize_counselor_name(b)
    ka, kb = na.replace(" ", ""), nb.replace(" ", "")
    if not ka or not kb:
        return False
    if ka == kb or ka in kb or kb in ka:
        return True
    if len(ka) >= 8 and len(ka) == len(kb):
        prefix = 0
        for x, y in zip(ka, kb):
            if x != y:
                break
            prefix += 1
        if prefix / len(ka) >= 0.8 and (len(ka) - prefix) <= 2:
            return True
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb:
        short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if short.issubset(long_):
            return True
    ta_sorted, tb_sorted = sorted(ta), sorted(tb)
    if len(ta_sorted) == len(tb_sorted) >= 2:
        exact_common = sum(1 for x in ta if x in tb)
        fuzzy_diffs = sum(1 for x, y in zip(ta_sorted, tb_sorted) if x != y and names_match_token(x, y))
        total_tokens = len(ta_sorted)
        if total_tokens == 2:
            return (exact_common + fuzzy_diffs) == 2
        else:
            return exact_common >= 2 and fuzzy_diffs <= 1
    return False


def names_match_token(x: str, y: str) -> bool:
    """تطابق دو توکن نام با تحمل یک تفاوت تک‌حرفی"""
    if x == y:
        return True
    if abs(len(x) - len(y)) > 1 or len(x) < 3:
        return False
    if len(x) == len(y):
        diff = sum(1 for cx, cy in zip(x, y) if cx != cy)
        return diff <= 1
    short_, long_ = (x, y) if len(x) < len(y) else (y, x)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short_:
            return True
    return False


def parse_age_bounds_perfect(age_str: str) -> Tuple[int, int]:
    if not age_str or not str(age_str).strip():
        return 0, 120
    s = str(age_str).strip()
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    for i, d in enumerate(persian_digits):
        s = s.replace(d, str(i))
    for w, num in NUM_WORDS.items():
        s = re.sub(r"\b" + w + r"\b", num, s)
    if "محدودیت خاصی ندارد" in s or "بدون محدودیت" in s:
        return 0, 120
    if "تا پایان نوجوانی" in s:
        return 0, 19
    all_nums = [int(n) for n in re.findall(r"\d+", s) if int(n) < 150]
    # فقط الگوی خرابِ بدون فاصله («۱۸الی۰۰۰») اینجا گیر می‌افتد؛ «۱۸ الی ۲۵»
    # باید به پارسر دامنه برسد و ۱۸ تا ۱۲۰ تفسیر نشود (سنِ ۴۰ را برای
    # مشاورِ مخصوصِ ۱۸-۲۵ مجاز می‌کرد)
    if "۱۸الی۰۰۰" in age_str or "18الی000" in s:
        return 18, 120
    if "به بعد" in s or "به بالا" in s or "بالای" in s:
        if all_nums:
            return min(all_nums), 120
        elif "نوجوانی" in s:
            return 12, 120
    m_range = re.search(r"(\d+)\s*(?:تا|الی|-)\s*(\d+)", s)
    if m_range:
        low, high = int(m_range.group(1)), int(m_range.group(2))
        if all_nums:
            return min(all_nums), max(all_nums)
        return low, high
    if "18 الی" in s:
        # «18 الی» بدون سقفِ دوم (ادامهٔ همان الگوی خراب) — بعد از محدودهٔ معتبر
        return 18, 120
    if "زیر" in s or "تا" in s:
        if all_nums:
            return 0, max(all_nums)
    if "نوجوان" in s and "بزرگسال" in s:
        return 12, 120
    if "نوجوان" in s:
        return 12, 19
    if "جوان" in s or "بزرگسال" in s:
        return 18, 120
    if "کودک" in s:
        return 0, 12
    return 0, 120


# ── یادگیری ──

def _weight_from_counts(pos: int, neg: int) -> float:
    """فرمول واحد وزن خودآموز: 1 + log1p(pos)·0.12 − neg·0.10 در بازه [0.6, 1.6]"""
    w = 1.0 + math.log1p(max(0, pos)) * LEARN_POS_COEF - max(0, neg) * LEARN_NEG_COEF
    return max(LEARN_WEIGHT_MIN, min(LEARN_WEIGHT_MAX, w))


def init_learning_db():
    """جدول یادگیری مفهومی: هر رکورد = (مشاور، مفهوم بالینی) با شمارنده موفقیت/شکست"""
    conn = sqlite3.connect(str(LEARNING_DB))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS concept_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER DEFAULT 1,
            counselor_name TEXT NOT NULL,
            concept TEXT NOT NULL,
            ghq_level TEXT DEFAULT 'normal',
            positive_signals INTEGER DEFAULT 0,
            negative_signals INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tenant_id, counselor_name, concept)
        )
    """)
    cols = [c[1] for c in cur.execute("PRAGMA table_info(concept_feedback)").fetchall()]
    if "tenant_id" not in cols:
        cur.execute("""
            CREATE TABLE concept_feedback_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER DEFAULT 1,
                counselor_name TEXT NOT NULL,
                concept TEXT NOT NULL,
                ghq_level TEXT DEFAULT 'normal',
                positive_signals INTEGER DEFAULT 0,
                negative_signals INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, counselor_name, concept)
            )
        """)
        cur.execute("""
            INSERT INTO concept_feedback_new (id, tenant_id, counselor_name, concept, ghq_level, positive_signals, negative_signals, updated_at)
            SELECT id, 1, counselor_name, concept, ghq_level, positive_signals, negative_signals, updated_at FROM concept_feedback
        """)
        cur.execute("DROP TABLE concept_feedback")
        cur.execute("ALTER TABLE concept_feedback_new RENAME TO concept_feedback")
    conn.commit()
    conn.close()


def _concept_from_topic(topic: str) -> str | None:
    t = (topic or "").lower()
    for concept, keywords in EXPANDED_SYNONYMS.items():
        if any(kw in t for kw in keywords):
            return concept
    return None


def get_learning_weight(counselor_name: str, concept: str | None = None, tenant_id: int = 1) -> float:
    if not concept:
        return 1.0
    try:
        conn = sqlite3.connect(str(LEARNING_DB))
        cur = conn.cursor()
        cur.execute(
            "SELECT positive_signals, negative_signals FROM concept_feedback WHERE tenant_id=? AND counselor_name=? AND concept=? LIMIT 1",
            (tenant_id, counselor_name, concept),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return 1.0
        return _weight_from_counts(row[0], row[1])
    except Exception:
        return 1.0


def record_learning_event(counselor_name: str, topic: str, ghq_level: str = "normal", success: bool = True, tenant_id: int = 1):
    concept = _concept_from_topic(topic)
    if not concept:
        return
    _record_concept_feedback(counselor_name, concept, ghq_level, success, tenant_id)


def record_feedback_direct(counselor_name: str, concept: str, success: bool, tenant_id: int = 1) -> bool:
    if concept not in EXPANDED_SYNONYMS:
        return False
    _record_concept_feedback(counselor_name, concept, "normal", success, tenant_id)
    return True


def _record_concept_feedback(counselor_name: str, concept: str, ghq_level: str, success: bool, tenant_id: int = 1):
    try:
        conn = sqlite3.connect(str(LEARNING_DB))
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO concept_feedback (tenant_id, counselor_name, concept, ghq_level, positive_signals, negative_signals)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, counselor_name, concept) DO UPDATE SET
                positive_signals = positive_signals + excluded.positive_signals,
                negative_signals = negative_signals + excluded.negative_signals,
                ghq_level = excluded.ghq_level,
                updated_at = CURRENT_TIMESTAMP
        """, (tenant_id, counselor_name, concept, ghq_level, 1 if success else 0, 0 if success else 1))
        conn.commit()
        conn.close()
    except Exception as e:
        # قبلاً pass بی‌صدا — یادگیری نباید گم شود بدون اطلاع
        logger.warning("concept feedback not recorded (%s/%s): %s", counselor_name, concept, e)


def get_learning_stats(tenant_id: int = 1) -> list:
    try:
        conn = sqlite3.connect(str(LEARNING_DB))
        rows = conn.execute(
            "SELECT counselor_name, concept, positive_signals, negative_signals FROM concept_feedback WHERE tenant_id=? ORDER BY updated_at DESC LIMIT 20",
            (tenant_id,),
        ).fetchall()
        conn.close()
        return [
            (n, c, p, ng, round(get_learning_weight(n, c, tenant_id), 2))
            for n, c, p, ng in rows
        ]
    except Exception:
        return []


# ── نوبت‌های آزاد ──

def load_free_appointments(tenant_id: int | None = None) -> List[Tuple[str, str]]:
    """نوبت‌های آزادِ یک مرکز — نه یک جدول سراسری.

    جدول appointments پیش از این ستون tenant_id نداشت و برای *همهٔ*
    مشتریان خوانده می‌شد، پس «🗓 نوبت آزاد: ۴ نوبت» و امتیازِ نوبتِ آزادِ
    یک مرکز به مراجعِ مرکزِ دیگر نشت می‌کرد. tenant_id=None یعنی
    «نامشخص» (tenant_id=0 در جدول) و رفتارِ محافظه‌کارانه دارد."""
    try:
        conn = sqlite3.connect(str(APPOINTMENTS_DB))
        if tenant_id is None:
            rows = conn.execute(
                "SELECT counselor_name, branch FROM appointments "
                "WHERE status='free' AND tenant_id=0").fetchall()
        else:
            rows = conn.execute(
                "SELECT counselor_name, branch FROM appointments "
                "WHERE status='free' AND tenant_id=?", (tenant_id,)).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _index_free_by_name(free_rows: List[Tuple[str, str]]) -> Dict[str, List[str]]:
    idx: Dict[str, List[str]] = {}
    for db_name, branch in free_rows:
        idx.setdefault(db_name, []).append((branch or "").lower())
    return idx


_name_match_cache: Dict[Tuple[str, str], bool] = {}
_name_match_cache_lock = threading.Lock()


def _names_match_cached(a: str, b: str) -> bool:
    key = (a, b) if a <= b else (b, a)
    cached = _name_match_cache.get(key)
    if cached is not None:
        return cached
    result = names_match(a, b)
    with _name_match_cache_lock:
        if len(_name_match_cache) > 50000:
            _name_match_cache.clear()
        _name_match_cache[key] = result
    return result


# گروه‌های alias شعبه‌های قدیمی — نام‌های دیگرِ همان شعبه در فایل خام (کرالر/اکسل)
_BRANCH_ALIAS_GROUPS = (
    (("ظفر", "زعفرانیه", "zafar"), ("zafar", "ظفر", "زعفرانیه")),
    (("ایران", "خیابان ایران", "iran"), ("iran", "ایران", "خیابان ایران")),
)


def _count_free_for_indexed(counselor_name: str, branch_pref: str, free_idx: Dict[str, List[str]]) -> int:
    pref = (branch_pref or "").strip()
    # شعبهٔ شناخته‌شده → تطبیق با همهٔ aliasهایش؛ وگرنه None (رفتار سفارشی/بی‌فیلتر)
    group_tokens = None
    for group, tokens in _BRANCH_ALIAS_GROUPS:
        if pref in group:
            group_tokens = tokens
            break
    neutral = pref in ("", "اهمیتی ندارد", "فرقی ندارد", "هر دو")
    cnt = 0
    for db_name, branches in free_idx.items():
        if not _names_match_cached(counselor_name, db_name):
            continue
        for b in branches:
            if group_tokens is not None:
                if any(t in b for t in group_tokens):
                    cnt += 1
            elif neutral:
                cnt += 1
            elif b and (pref in b or b in pref):
                # شعبهٔ سفارشی تعریف‌شدهٔ ادمین — تطابق دوطرفهٔ زیررشته‌ای
                cnt += 1
    return cnt


def _count_free_for(counselor_name: str, branch_pref: str, free_rows: List[Tuple[str, str]]) -> int:
    return _count_free_for_indexed(counselor_name, branch_pref, _index_free_by_name(free_rows))


def _tenant_learns(tenant_id: int) -> bool:
    """پلنِ مشتری اجازهٔ یادگیری از بازخورد را می‌دهد؟ (حرفه‌ای/کامل)

    موتور قاعده‌ای زیرِ همهٔ پلن‌ها اجرا می‌شود، پس بدون این گیت، وزن‌های
    یادگیریِ یک دورهٔ حرفه‌ای بعد از تنزل به پایه هم اثر می‌کردند — چیزی
    که پلن پایه نمی‌فروشد."""
    try:
        from app import plan_service
        return plan_service.tenant_has_mode(tenant_id, plan_service.MODE_ADVANCED)
    except Exception:
        return False


def load_learning_weights(tenant_id: int) -> Dict[Tuple[str, str], float]:
    try:
        conn = sqlite3.connect(str(LEARNING_DB))
        rows = conn.execute(
            "SELECT counselor_name, concept, positive_signals, negative_signals FROM concept_feedback WHERE tenant_id=?",
            (tenant_id,),
        ).fetchall()
        conn.close()
        return {(name, concept): _weight_from_counts(pos, neg) for name, concept, pos, neg in rows}
    except Exception:
        return {}


# ── قواعد امتیازدهی (ترتیب = رفتار تاریخی؛ دلایل نمایش‌داده‌شده از اولین موارد انتخاب می‌شوند) ──

# (کلیدهای موضوع، کلیدهای متن مشاور، امتیاز، دلیل)
_EXACT_TOPIC_RULES = [
    (["جنس مخالف"], ["جنس مخالف"], 50.0, "تخصص ویژه در روابط عاطفی و ارتباط با جنس مخالف"),
    (["جنسی", "سکس", "سکستراپی", "واژینیسموس", "زناشویی"],
     ["جنسی", "سکس", "سکستراپی", "مامایی"], 45.0, "تخصص در سلامت جنسی و مشکلات زناشویی"),
    (["کنکور", "انتخاب رشته", "تحصیلی", "برنامه ریزی"],
     ["کنکور", "انتخاب رشته", "تحصیلی", "برنامه ریزی"], 45.0,
     "مشاوره تخصصی تحصیلی، کنکور و هدایت شغلی"),
    (["یادگیری", "املا", "ریاضی", "دیکته", "نقص توجه", "بیش فعالی", "adhd"],
     ["یادگیری", "نقص توجه", "بیش فعالی", "adhd"], 45.0,
     "درمان تخصصی اختلالات یادگیری و بیش‌فعالی (ADHD)"),
]

# (مفهوم، کلیدهای متن مشاور، امتیاز، دلیل)
# دو لیست نام‌دار به‌جای برش positional: قواعد سنی (نوجوان/کودک) بین این دو اجرا می‌شوند
_CONCEPT_RULES_PRE_AGE = [
    ("حقوقی", ["وکالت", "حقوقی", "دادگاه", "حقوق خانواده"], 80.0, "مشاوره تخصصی حقوقی خانواده"),
    ("پزشکی_روانپزشکی", ["روان پزشکان", "اعصاب و روان", "دارو", "طب ایرانی"], 80.0, "متخصص اعصاب و روان (روانپزشک)"),
    ("والد_فرزند", ["والد", "فرزند", "والدگری", "خانواده"], 50.0, "تخصص در بهبود روابط و تعارضات والد و فرزند"),
    ("ارتباط_تعارض", ["بین‌فردی", "بین فردی", "روابط", "ارتباط", "تعارض", "مهارت"], 45.0,
     "تخصص در مهارت‌های ارتباطی و حل تعارضات بین‌فردی"),
]

_CONCEPT_RULES_POST_AGE = [
    ("زوج_ازدواج", ["زوج", "ازدواج", "زناشویی", "همسر"], 45.0, "تخصص در زوج‌درمانی و مشاوره پیش از ازدواج"),
    ("افسردگی", ["افسردگی", "خلق", "خلقی", "سوگ", "طرحواره"], 40.0, "درمان تخصصی افسردگی و افت انگیزه"),
    ("وسواس", ["وسواس", "وسواسی", "ocd"], 45.0, "درمان تخصصی وسواس فکری و عملی (OCD)"),
    ("اضطراب", ["اضطراب", "استرس", "پانیک", "فوبیا"], 40.0, "درمان تخصصی اضطراب و استرس"),
    ("توسعه_فردی", ["توسعه فردی", "عزت نفس", "کوچینگ", "کمال"], 35.0, "مشاوره توسعه فردی و عزت‌نفس"),
]

DEFAULT_REASON = "تطابق با رده سنی، موضوع مراجع و تخصص‌های مرکز"

# لایهٔ تخصصِ مشاور — تنها سیگنالِ متمایزکننده‌ای که از خودِ پروفایل می‌آید و
# برای دو مشاورِ هم‌موضوع متفاوت است. دلیل‌های مبتنی بر «موضوعِ مراجع» برای هر دو
# مشاورِ هم‌موضوع عیناً یکسان درمی‌آید و مراجع می‌پرسد «پس فرق این دوتا چیست؟».
# ترتیب = از خاص‌ترین به عمومی‌ترین؛ اولین تطابق برنده است.
_SPEC_TIERS: List[Tuple[str, str]] = [
    ("بالینی", "تجربهٔ کار بالینی و کار با مراجعان روان‌شناختیِ ارزیابی‌شده"),
    ("روان‌پزشک", "همکاری با روان‌پزشک و تجربهٔ ارجاع‌های بالینی"),
    ("اعصاب", "تجربهٔ کار با مراجعان دارویی و ارجاع‌های روان‌پزشکی"),
    ("کودک", "تخصص اختصاصی در روان‌شناسی کودک و فرزندپروری"),
    ("نوجوان", "تخصص اختصاصی در مشاورهٔ نوجوان و جوان"),
    ("مدرسه", "تجربهٔ مشاورهٔ مدرسه و هدایت تحصیلی"),
    ("کودکان", "تخصص اختصاصی در روان‌شناسی کودک و فرزندپروری"),
    ("خانواده", "تخصص در مشاورهٔ خانواده و روابط والد و فرزند"),
    ("ازدواج", "تخصص در مشاورهٔ زوجین و مسائل زناشویی"),
    ("سکس", "تخصص در سلامت جنسی و مشکلات زناشویی"),
    ("شغلی", "تخصص در مشاورهٔ شغلی و انتخاب رشته"),
    ("تحصیلی", "تخصص در مشاورهٔ تحصیلی و یادگیری"),
    ("اعتیاد", "تخصص در مشاورهٔ اعتیاد و رفتارهای آسیب‌زن"),
    ("پس از زایمان", "تخصص در سلامت روان پس از زایمان"),
    ("خشم", "تخصص در مدیریت خشم و کنترل تکانه"),
    ("اعتماد", "تخصص در عزت‌نفس و اعتماد به نفس"),
    ("کوچینگ", "تخصص در کوچینگ و مهارت‌های زندگی"),
    ("اعصاب و روان", "تجربهٔ کار با مراجعان روان‌پزشکی"),
    ("تحصیل", "تخصص در مشاورهٔ تحصیلی و هدایت درسی"),
]

# نقطهٔ اصلاح: «تخصص در این حوزه» برای دو مشاور یکسان بود. هر ردهٔ سابقه یک عبارت
# متفاوت می‌گیرد تا گزینهٔ اول «چرا این؟» و گزینهٔ دوم «چرا آن؟» را جدا جواب دهد.
_ABILITY_TIERS: List[Tuple[float, str, str]] = [
    (2.5, "سابقهٔ حرفه‌ای بالای ۲۰ سال", "سابقهٔ حرفه‌ای ۱۰ تا ۲۰ سال"),
    (1.5, "سابقهٔ حرفه‌ای ۱۰ تا ۲۰ سال", "سابقهٔ حرفه‌ای ۵ تا ۱۰ سال"),
    (0.5, "سابقهٔ حرفه‌ای ۵ تا ۱۰ سال", "سابقهٔ حرفه‌ای ۳ تا ۵ سال"),
]

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def detect_spec_tiers(specs_text: str) -> list[str]:
    """عبارت‌های تخصصِ متمایز برای این مشاور — به ترتیب اولویت

    چند مورد برمی‌گردد تا اگر دو مشاورِ رتبه‌یک تخصصِ مشترک داشتند، گزینهٔ دوم
    بتواند به تخصصِ بعدیِ خودش برود."""
    out: list[str] = []
    for needle, phrase in _SPEC_TIERS:
        if needle in specs_text and phrase not in out:
            out.append(phrase)
    return out


def ability_phrase(ability: float) -> str:
    """سابقهٔ مشاور به عبارت — سه ردهٔ متمایز، نه یک عبارتِ واحد برای همه"""
    for lo, high_phrase, low_phrase in _ABILITY_TIERS:
        if ability >= lo:
            return high_phrase
    return _ABILITY_TIERS[-1][2]


def _trigger_concepts(user_topic: str) -> set:
    triggered = set()
    for concept, keywords in EXPANDED_SYNONYMS.items():
        if any(kw in user_topic for kw in keywords):
            triggered.add(concept)
    return triggered


def _passes_hard_filters(p: dict, *, gender_pref: str, branch: str,
                         branch_filter_active: bool, use_age: bool, use_branch: bool,
                         user_age: int, is_legal_request: bool) -> bool:
    """فیلتر صلب/آبشاری — تک تابع با پرچم (قبلاً ۳ حلقه تقریباً یکسان)"""
    if gender_pref in ["خانم", "زن"] and p["gender"] != "خانم":
        return False
    if gender_pref in ["آقا", "مرد"] and p["gender"] != "آقا":
        return False
    if use_branch and branch_filter_active:
        loc = str(p.get("location", "")).strip()
        if loc != "هر دو" and branch not in loc:
            return False
    if use_age and user_age > 0:
        if not (p["age_min"] - AGE_SLACK <= user_age <= p["age_max"] + AGE_SLACK):
            return False
    if not is_legal_request and (
        "وکالت" in p.get("education_experience", "")
        or p.get("active_specs") == ["مشاوره حقوقی خانواده"]
    ):
        return False
    return True


def _filter_candidates(profiles: list, *, gender_pref: str, branch: str,
                       user_age: int, is_legal_request: bool) -> list:
    """آبشار: کامل → بدون سن → بدون شعبه (جنسیت و حقوقی همیشه می‌مانند)"""
    # هر شعبهٔ صریح (از جمله شعبهٔ تازه تعریف‌شدهٔ ادمین) فیلتر را فعال می‌کند؛
    # فقط گزینه‌های «بی‌تفاوت» فیلتر ندارند
    branch_filter_active = branch not in ("", "اهمیتی ندارد", "فرقی ندارد", "هر دو")

    def _filt(use_age: bool, use_branch: bool) -> list:
        return [
            p for p in profiles
            if _passes_hard_filters(
                p, gender_pref=gender_pref, branch=branch,
                branch_filter_active=branch_filter_active,
                use_age=use_age, use_branch=use_branch,
                user_age=user_age, is_legal_request=is_legal_request,
            )
        ]

    candidates = _filt(use_age=True, use_branch=True)
    if not candidates:
        candidates = _filt(use_age=False, use_branch=True)
    if not candidates:
        candidates = _filt(use_age=False, use_branch=False)
    return candidates


def filter_profiles_for_user(user_info: dict, profiles: list) -> list:
    """فیلترهای صلب برای مسیر LLM — جنسیت/شعبه/سن/حقوقی باید در هر دو
    موتور یکسان رعایت شوند؛ وگرنه پلن پولی از weak سست‌تر می‌شود و LLM
    می‌توانست خارج از شرایط صریح مراجع، مشاور برگرداند (فقط نام grounding
    می‌شد، نه شرایط)."""
    user_age = int(user_info.get("age") or 0)
    gender_pref = user_info.get("preferred_gender") or "فرقی ندارد"
    user_branch = user_info.get("branch") or "اهمیتی ندارد"
    user_topic = (
        (user_info.get("topic") or "") + " " + (user_info.get("expectation") or "")
    ).lower()
    is_legal_request = "حقوقی" in _trigger_concepts(user_topic)
    return _filter_candidates(
        profiles, gender_pref=gender_pref, branch=user_branch,
        user_age=user_age, is_legal_request=is_legal_request,
    )


def _score_candidate(p: dict, ctx: dict) -> tuple[float, list[str], int]:
    """pipeline امتیازدهی — خروجی: (امتیاز نهایی، دلایل تمیز، تعداد نوبت آزاد)"""
    text = p["search_text"]
    score = 0.0
    reasons: list[str] = []

    # کلمات موضوع در متن (+بدون دلیل)
    for w in ctx["user_words"]:
        if w in text:
            score += SCORE_KEYWORD_HIT

    # قواعد دقیق موضوع↔متن
    for topic_kws, text_kws, pts, reason in _EXACT_TOPIC_RULES:
        if any(w in ctx["user_topic"] for w in topic_kws) and any(w in text for w in text_kws):
            score += pts
            reasons.append(reason)

    # قواعد مفهومی — قبل از قواعد سنی
    for concept, text_kws, pts, reason in _CONCEPT_RULES_PRE_AGE:
        if concept in ctx["triggered"] and any(w in text for w in text_kws):
            score += pts
            reasons.append(reason)

    # قواعد سنی (نوجوان/کودک) — بین دو لیست مفهومی (ترتیب تاریخی)
    if ("نوجوان_جوان" in ctx["triggered"] or 12 <= ctx["user_age"] <= 19) and \
            any(w in text for w in ["نوجوان", "نوجوانی", "جوان"]):
        score += 35.0
        reasons.append("مشاوره تخصصی رده سنی نوجوان و جوان")
    if ("کودک" in ctx["triggered"] or (0 < ctx["user_age"] < 12)) and \
            any(w in text for w in ["کودک", "کودکان", "فرزندپروری", "بازی"]):
        score += 40.0
        reasons.append("تخصص در روانشناسی کودک و فرزندپروری")

    # قواعد مفهومی — بعد از قواعد سنی
    for concept, text_kws, pts, reason in _CONCEPT_RULES_POST_AGE:
        if concept in ctx["triggered"] and any(w in text for w in text_kws):
            score += pts
            reasons.append(reason)

    # GHQ (آستانه‌ها از ghq_analyzer)
    if ctx["ghq_active"]:
        if ctx["is_high_risk"] and ("روان پزشکان" in text or "اعصاب و روان" in text):
            score += SCORE_GHQ_PSYCHIATRIST
            reasons.append("ارزیابی بالینی و روانپزشکی بر اساس غربالگری GHQ")
        elif ctx["is_high_risk"] and ("بالینی" in text or "رواندرمانگر" in text):
            score += SCORE_GHQ_CLINICAL
            reasons.append("ارزیابی روان‌شناختی تخصصی بر اساس غربالگری GHQ")
        if ctx["depression"] >= SCORE_GHQ_SUBSCALE_THRESHOLD and ("افسردگی" in text or "خلق" in text):
            score += ctx["depression"] * SCORE_GHQ_SUBSCALE_PER_POINT
        if ctx["anxiety"] >= SCORE_GHQ_SUBSCALE_THRESHOLD and ("اضطراب" in text or "وسواس" in text):
            score += ctx["anxiety"] * SCORE_GHQ_SUBSCALE_PER_POINT

    score += float(p.get("ability", 2.0)) * SCORE_ABILITY_MULTIPLIER

    free_slots = _count_free_for_indexed(p["clean_name"], ctx["location_pref"], ctx["free_idx"])
    if free_slots > 0:
        score += min(free_slots * SCORE_FREE_SLOT_POINT, SCORE_FREE_SLOT_CAP)
        reasons.append("نوبت آزاد در دسترس")

    # وزن خودآموز: ضرب در همه مفاهیم فعال این مشاور
    boost = 1.0
    for concept in ctx["triggered"]:
        w = ctx["learning_w"].get((p["clean_name"], concept), 1.0)
        if w != 1.0:
            boost *= w
    final_score = score * boost

    clean: list[str] = []
    for r in reasons:
        if r not in clean:
            clean.append(r)

    # متمایزکردن دلیل‌ها بین دو گزینه، کارِ _differentiate است نه اینجا: هر گزینه
    # باید متمایزکنندهٔ متفاوت بگیرد و اینجنا هر دو یک رژیمِ یکسان می‌گیرند.
    return round(final_score, 2), clean, free_slots


_FILLER_WORDS = {"در", "و", "یا", "بر", "به", "این", "آن", "های", "ها",
                 "اختصاصی", "تخصص", "حوزه", "را", "از", "که", "برای", "با"}


def _key_terms(text: str) -> set:
    """توکن‌های محتوایی یک عبارت — برای تشخیص «عملاً همان حرف» با واژهٔ دیگر"""
    t = text.replace("‌", " ").replace("‌", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return {w for w in t.split() if len(w) > 2 and w not in _FILLER_WORDS}


def _too_similar(a: str, b: str) -> bool:
    ta, tb = _key_terms(a), _key_terms(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def _pick_differentiator(rec: dict, why: str, used: set) -> str:
    """اولین متمایزکنندهٔ این مشاور که واقعاً چیزِ تازه‌ای می‌گوید"""
    tiers = rec.get("spec_tiers") or []
    for phrase in [*tiers, rec.get("ability_phrase") or ""]:
        if (phrase and phrase not in used
                and not _too_similar(phrase, why)
                and all(not _too_similar(phrase, r) for r in rec["reasons"])):
            return phrase
    return ("دارای مجوز رسمی سازمان نظام روان‌شناسی و مشاوره"
            if rec.get("licensed") else DEFAULT_REASON)


def _differentiate(recs: list[dict]) -> list[dict]:
    """ساخت متنِ دلیل برای گزینه‌های معرفی — هر گزینه یک متمایزکنندهٔ متفاوت.

    دلیل‌های مبتنی بر موضوعِ مراجع برای دو مشاورِ هم‌موضوع عیناً یکسان‌اند و مراجع
    می‌پرسد «پس فرق این دوتا چیست؟». نمایش دو تکه است: «چرا این؟» (موضوع) و
    «چه چیزیِ متفاوتی دارد؟». متمایزکننده بین جفت انتخاب می‌شود، نه بین هر مشاور
    مستقل — وگرنه دو مشاور با تخصص مشترک باز هم یک متن می‌گیرند."""
    used: set[str] = set()
    out = []
    for rec in recs:
        rec = dict(rec)
        why = rec["reasons"][0]
        diff = _pick_differentiator(rec, why, used)
        used.add(diff)
        rec["reason"] = f"{why}، {diff}"
        rec.pop("reasons", None)
        out.append(rec)
    return out


class SpiralMatchEngine:
    def __init__(self, profiles_path: Path = PROFILES_JSON):
        self.profiles_path = profiles_path
        # ارزش: (پروفایل‌ها، monotonic_time) — TTL برای ناهمگامی بین workerها
        self._tenant_cache: Dict[int, Tuple[List[Dict[str, Any]], float]] = {}
        self._cache_lock = threading.Lock()
        self._last_panel_tenant = 1
        init_learning_db()

    def reload(self, tenant_id: int = 1):
        with self._cache_lock:
            self._tenant_cache.pop(tenant_id, None)

    def set_tenant(self, tenant_id: int, force: bool = False):
        self._last_panel_tenant = tenant_id
        self.get_profiles(tenant_id, force=force)

    @property
    def profiles(self) -> List[Dict[str, Any]]:
        return self.get_profiles(self._last_panel_tenant)

    @profiles.setter
    def profiles(self, value):
        pass

    def get_profiles(self, tenant_id: int, force: bool = False) -> List[Dict[str, Any]]:
        """پروفایل‌ها با کش TTL — worker دیگر حداکثر ۶۰ ثانیه کهنه می‌بیند"""
        now = time.monotonic()
        if not force:
            cached = self._tenant_cache.get(tenant_id)
            if cached is not None and (now - cached[1]) < PROFILE_CACHE_TTL:
                return cached[0]
        with self._cache_lock:
            now = time.monotonic()
            if not force:
                cached = self._tenant_cache.get(tenant_id)
                if cached is not None and (now - cached[1]) < PROFILE_CACHE_TTL:
                    return cached[0]
            profiles = self._load_tenant_profiles(tenant_id)
            self._tenant_cache[tenant_id] = (profiles, time.monotonic())
            return profiles

    def _load_tenant_profiles(self, tenant_id: int) -> List[Dict[str, Any]]:
        try:
            conn = sqlite3.connect(str(USER_RECORDS_DB))
            try:
                cur = conn.cursor()
                cols = [c[1] for c in cur.execute("PRAGMA table_info(consultants)").fetchall()]
                if "tenant_id" not in cols:
                    return self._load_and_index_profiles()
                rows = cur.execute(
                    "SELECT name, ability, location, education_experience, general_area, general_area_2, specializations, detailed_topics, age_range, license, notes FROM consultants WHERE tenant_id=?",
                    (tenant_id,),
                ).fetchall()
            finally:
                conn.close()
            if not rows and tenant_id == 1:
                return self._load_and_index_profiles()
        except Exception:
            # خطای دیتابیس نباید فهرستِ مستأجرِ دیگر را جای این مستأجر بگذارد —
            # خالی یعنی «معرفی نداریم» (لید همچنان ثبت می‌شود)؛ نشت رقیب نه
            logger.exception("load tenant profiles failed tenant=%s", tenant_id)
            return []
        raw_profiles = []
        for (name, ability, location, edu, ga, ga2, specs_json, det, age_range, lic, notes) in rows:
            try:
                specs = json.loads(specs_json) if specs_json else {}
            except Exception:
                specs = {}
            raw_profiles.append({
                "name": name, "ability": ability or 2.0, "location": location or "هر دو",
                "education_experience": edu or "", "general_area": ga or "", "general_area_2": ga2 or "",
                "specializations": specs, "detailed_topics": det or "",
                "age_range": age_range or "", "license": lic or "", "notes": notes or "",
            })
        return self._index_profiles(raw_profiles)

    def _load_and_index_profiles(self) -> List[Dict[str, Any]]:
        if not self.profiles_path.exists():
            return []
        with open(self.profiles_path, "r", encoding="utf-8") as f:
            raw_profiles = json.load(f)
        return self._index_profiles(raw_profiles)

    def _index_profiles(self, raw_profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        indexed = []
        for p in raw_profiles:
            name = p.get("name", "").strip()
            gender = get_consultant_gender(name)
            age_min, age_max = parse_age_bounds_perfect(p.get("age_range", ""))
            ability = float(p.get("ability", 2.0))
            if ability not in [1.0, 2.0, 3.0]:
                ability = 2.0
            loc = p.get("location", "هر دو")
            specs_list = list(p.get("specializations", {}).keys()) if isinstance(p.get("specializations"), dict) else []
            combined_text = f"{p.get('education_experience', '')} {p.get('general_area', '')} {p.get('general_area_2', '')} {' '.join(specs_list)} {' '.join(p.get('specializations', {}).values()) if isinstance(p.get('specializations'), dict) else ''} {p.get('detailed_topics', '')} {p.get('license', '')} {p.get('notes', '')}".lower()
            indexed.append({
                **p,
                "clean_name": name,
                "gender": gender,
                "age_min": age_min,
                "age_max": age_max,
                "ability": ability,
                "location": loc,
                "search_text": combined_text,
                "active_specs": specs_list,
            })
        return indexed

    def match(self, user_info: Dict[str, Any], tenant_id: int = 1) -> List[Dict[str, Any]]:
        user_age = int(user_info.get("age", 0))
        gender_pref = user_info.get("preferred_gender", "فرقی ندارد")
        user_branch = user_info.get("branch", "اهمیتی ندارد")
        location_pref = user_info.get("location", "") or user_branch
        user_topic = (user_info.get("topic", "") + " " + user_info.get("expectation", "")).lower()
        ghq = user_info.get("ghq_scores")

        triggered = _trigger_concepts(user_topic)
        is_legal_request = "حقوقی" in triggered
        profiles = self.get_profiles(tenant_id)

        # ۱) فیلتر آبشاری
        candidates = _filter_candidates(
            profiles, gender_pref=gender_pref, branch=user_branch,
            user_age=user_age, is_legal_request=is_legal_request,
        )

        # ۲) زمینه امتیازدهی (یک‌بار)
        ghq_active = bool(ghq and isinstance(ghq, dict) and "total" in ghq)
        ctx = {
            "triggered": triggered,
            "user_topic": user_topic,
            "user_words": [w for w in re.findall(r"\w+", user_topic) if len(w) > 2],
            "user_age": user_age,
            "location_pref": location_pref,
            "free_idx": _index_free_by_name(load_free_appointments(tenant_id)),
            # وزن‌های خودآموز فقط برای پلن‌های یادگیرنده (حرفه‌ای/کامل).
            # نوشتنِ بازخورد گیت دارد، ولی خواندنِ وزن‌ها — که در امتیازدهی
            # زیرِ همهٔ پلن‌ها اعمال می‌شد — نداشت؛ پس بعد از تنزلِ مشتری به
            # پایه، وزن‌های به‌جاماندهٔ دورهٔ حرفه‌ای هنوز توصیه‌ها را جابه‌جا می‌کردند.
            "learning_w": (
                load_learning_weights(tenant_id)
                if _tenant_learns(tenant_id) else {}
            ),
            "ghq_active": ghq_active,
            "is_high_risk": ghq_active and (
                ghq.get("total", 0) >= GHQ_SEVERE_TOTAL
                or ghq.get("depression", 0) >= GHQ_SEVERE_DEPRESSION
            ),
            "depression": ghq.get("depression", 0) if ghq_active else 0,
            "anxiety": ghq.get("anxiety", 0) if ghq_active else 0,
        }

        # ۳) امتیازدهی
        scored: list[dict] = []
        for p in candidates:
            final_score, clean_reasons, free_slots = _score_candidate(p, ctx)
            scored.append({
                "name": p["clean_name"],
                "score": final_score,
                "ability": p["ability"],
                "free_slots": free_slots,
                "gender": p["gender"],
                "location": p["location"],
                "reasons": clean_reasons or [DEFAULT_REASON],
                # متمایزکننده‌های پروفایل — فقط برای متن نهایی، نه برای امتیاز
                "spec_tiers": detect_spec_tiers(
                    " ".join([p.get("general_area", ""), p.get("general_area_2", ""),
                              " ".join(p.get("active_specs", [])), p.get("detailed_topics", "")]).lower()
                ),
                "ability_phrase": ability_phrase(float(p.get("ability", 2.0))),
                "licensed": str(p.get("license", "")).strip().startswith("بله"),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return _differentiate(scored[:TOP_N_RESULTS])


# نمونه سراسری موتور — در lifespan مقداردهی اولیه می‌شود
engine = SpiralMatchEngine()
