# -*- coding: utf-8 -*-
"""
Nikravan Internal Spiral AI Engine & Clinical Decision Matcher
Internal AI, spiral-matching and clinical triage engine for the Nikravan Family Counseling Center
"""

import os
import re
import json
import sqlite3
import math
import threading
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

from app.config import PROFILES_JSON, APPOINTMENTS_DB, AI_KNOWLEDGE_DB, USER_RECORDS_DB
LEARNING_DB = AI_KNOWLEDGE_DB

MALE_COUNSELORS = {
    "محمدعلی نوری", "سید مجید ذاکری", "سید امیر زرباف", "محمدابراهیم کلباسی",
    "حمید رضا افسری", "محمدعلی رفیق‌دوست", "حامد مجدی", "مهدی شمس الاحرار فرد",
    "شاهین رضا ادیبی", "جلایی فر", "محمدباقردربندی", "محمدصادق رمضانی زاده",
    "رضا غفارزاده نمازی", "اسماعیل اسماعیلی شریف", "محمود گلزاری", "محمد رضا کمن",
    "عباس باقری", "امیر حسین طهرانچی", "سید ایمان مصلح طهرانی", "حسین مینایی نیا",
    "مصطفی غریب", "سجاد جلایی فر"
}

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
    return "خانم"


# Normalize the name so Excel profiles match the appointment-site records
_TITLE_PATTERNS = [r"\bآقای\b", r"\bخانم\b", r"\bدکتر\b", r"\bدكتر\b", r"^اپراتور\s*\d*"]


def normalize_counselor_name(name: str) -> str:
    s = " ".join(str(name).split())
    for pat in _TITLE_PATTERNS:
        s = re.sub(pat, "", s)
    s = " ".join(s.split())
    # Unify zero-width/space separators and Arabic yeh/kaf letters
    s = s.replace("ي", "ی").replace("ك", "ک").replace("‌", " ").replace("‌", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def names_match(a: str, b: str) -> bool:
    """Fuzzy match of two counselor names: normalize + strip spaces + partial/spacing-tolerant match"""
    na, nb = normalize_counselor_name(a), normalize_counselor_name(b)
    ka, kb = na.replace(" ", ""), nb.replace(" ", "")
    if not ka or not kb:
        return False
    if ka == kb or ka in kb or kb in ka:
        return True
    # Single-token names with a long shared prefix (80%+) and equal length: e.g. Mahsa AmidiBiki / Mahsa AmidiBigi
    if len(ka) >= 8 and len(ka) == len(kb):
        prefix = 0
        for x, y in zip(ka, kb):
            if x != y:
                break
            prefix += 1
        if prefix / len(ka) >= 0.8 and (len(ka) - prefix) <= 2:
            return True
    # Token-based match: every token of the shorter set must appear in the longer set
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb:
        short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if short.issubset(long_):
            return True
    # Match tolerating one differing letter in a token (Karbala'i/Karbalayi, Biki/Bigi)
    # Safety condition: at least 2 other tokens must match exactly to avoid false positives
    # (e.g. "Zahra Mojahedi" and "Zahra Nili" must not match)
    ta_sorted, tb_sorted = sorted(ta), sorted(tb)
    if len(ta_sorted) == len(tb_sorted) >= 2:
        exact_common = sum(1 for x in ta if x in tb)
        fuzzy_diffs = sum(1 for x, y in zip(ta_sorted, tb_sorted) if x != y and names_match_token(x, y))
        total_tokens = len(ta_sorted)
        # For 2-token names: both tokens must match (exactly or fuzzily)
        # For 3+: at least 2 exact common tokens + at most 1 fuzzy token
        if total_tokens == 2:
            return (exact_common + fuzzy_diffs) == 2
        else:
            return exact_common >= 2 and fuzzy_diffs <= 1
    return False


def names_match_token(x: str, y: str) -> bool:
    """Match two name tokens tolerating a single-letter difference"""
    if x == y:
        return True
    if abs(len(x) - len(y)) > 1 or len(x) < 3:
        return False
    # One swapped/different letter at equal length
    if len(x) == len(y):
        diff = sum(1 for cx, cy in zip(x, y) if cx != cy)
        return diff <= 1
    # One extra/missing letter (Karbala'i/Karbalayi, Rafiqdoost/Rafiq doost)
    short_, long_ = (x, y) if len(x) < len(y) else (y, x)
    for i in range(len(long_)):
        if long_[:i] + long_[i+1:] == short_:
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
        s = re.sub(r'\b' + w + r'\b', num, s)
    if "محدودیت خاصی ندارد" in s or "بدون محدودیت" in s:
        return 0, 120
    if "تا پایان نوجوانی" in s:
        return 0, 19
    all_nums = [int(n) for n in re.findall(r'\d+', s) if int(n) < 150]
    if "۱۸الی۰۰۰" in age_str or "18الی000" in s or "18 الی" in s:
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


def init_learning_db():
    """Concept learning table: each row = (counselor, clinical concept) with success/failure counters"""
    conn = sqlite3.connect(str(LEARNING_DB))
    cur = conn.cursor()
    # The old table (if any) is kept; the new concept table is created
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
    """Detect the clinical concept from the user's free-text topic"""
    t = (topic or "").lower()
    for concept, keywords in EXPANDED_SYNONYMS.items():
        if any(kw in t for kw in keywords):
            return concept
    return None


def get_learning_weight(counselor_name: str, concept: str | None = None, tenant_id: int = 1) -> float:
    """
    Learning weight for (tenant, counselor, concept).
    Returns 1.0 (neutral) when this pair has no history.
    """
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
        pos, neg = row
        # Logarithmic growth with successes, linear decay with failures; range [0.6, 1.6]
        w = 1.0 + math.log1p(pos) * 0.12 - neg * 0.10
        return max(0.6, min(1.6, w))
    except Exception:
        return 1.0


def record_learning_event(counselor_name: str, topic: str, ghq_level: str = "normal", success: bool = True, tenant_id: int = 1):
    """Record a per-tenant concept learning event: reinforce/weaken the (counselor, topic-concept) pair"""
    concept = _concept_from_topic(topic)
    if not concept:
        return
    _record_concept_feedback(counselor_name, concept, ghq_level, success, tenant_id)


def record_feedback_direct(counselor_name: str, concept: str, success: bool, tenant_id: int = 1) -> bool:
    """
    Record direct admin feedback without needing topic text (per-tenant).
    Returns: whether the operation succeeded (False if the concept is invalid)
    """
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
    except Exception:
        pass


def get_learning_stats(tenant_id: int = 1) -> list:
    """Learning stats for the admin (per-tenant): (counselor, concept, positive, negative, current weight)"""
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


def load_free_appointments() -> List[Tuple[str, str]]:
    """Read all free appointments once (to avoid repeated connections during matching)"""
    try:
        conn = sqlite3.connect(str(APPOINTMENTS_DB))
        rows = conn.execute("SELECT counselor_name, branch FROM appointments WHERE status='free'").fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _index_free_by_name(free_rows: List[Tuple[str, str]]) -> Dict[str, List[str]]:
    """Group free appointments: site name → list of branches (fuzzy match once per unique name)"""
    idx: Dict[str, List[str]] = {}
    for db_name, branch in free_rows:
        idx.setdefault(db_name, []).append((branch or "").lower())
    return idx


_name_match_cache: Dict[Tuple[str, str], bool] = {}
_name_match_cache_lock = threading.Lock()


def _names_match_cached(a: str, b: str) -> bool:
    """Cached names_match — name pairs repeat and the function is expensive"""
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


def _count_free_for_indexed(counselor_name: str, branch_pref: str, free_idx: Dict[str, List[str]]) -> int:
    cnt = 0
    for db_name, branches in free_idx.items():
        if not _names_match_cached(counselor_name, db_name):
            continue
        for b in branches:
            if branch_pref in ["ظفر", "زعفرانیه", "zafar"]:
                if "zafar" in b or "ظفر" in b or "زعفرانیه" in b:
                    cnt += 1
            elif branch_pref in ["ایران", "خیابان ایران", "iran"]:
                if "iran" in b or "ایران" in b:
                    cnt += 1
            else:
                cnt += 1
    return cnt


def _count_free_for(counselor_name: str, branch_pref: str, free_rows: List[Tuple[str, str]]) -> int:
    return _count_free_for_indexed(counselor_name, branch_pref, _index_free_by_name(free_rows))


def get_free_appointments_count(counselor_name: str, branch_pref: str = "") -> int:
    return _count_free_for(counselor_name, branch_pref, load_free_appointments())


def load_learning_weights(tenant_id: int) -> Dict[Tuple[str, str], float]:
    """Read all of the tenant's learning weights at once: (counselor, concept) → weight"""
    try:
        conn = sqlite3.connect(str(LEARNING_DB))
        rows = conn.execute(
            "SELECT counselor_name, concept, positive_signals, negative_signals FROM concept_feedback WHERE tenant_id=?",
            (tenant_id,),
        ).fetchall()
        conn.close()
        out = {}
        for name, concept, pos, neg in rows:
            w = 1.0 + math.log1p(pos) * 0.12 - neg * 0.10
            out[(name, concept)] = max(0.6, min(1.6, w))
        return out
    except Exception:
        return {}


class SpiralMatchEngine:
    def __init__(self, profiles_path: Path = PROFILES_JSON):
        self.profiles_path = profiles_path
        self._tenant_cache: Dict[int, List[Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._last_panel_tenant = 1
        init_learning_db()

    def reload(self, tenant_id: int = 1):
        """Drop from cache — the next call reads fresh data from the DB"""
        with self._cache_lock:
            self._tenant_cache.pop(tenant_id, None)

    def set_tenant(self, tenant_id: int, force: bool = False):
        """Compatibility mode: warms the cache; matching no longer depends on it"""
        self._last_panel_tenant = tenant_id
        self.get_profiles(tenant_id, force=force)

    @property
    def profiles(self) -> List[Dict[str, Any]]:
        """Compatibility: profiles of the last tenant viewed in the panel — display only"""
        return self.get_profiles(self._last_panel_tenant)

    @profiles.setter
    def profiles(self, value):
        pass  # no more global state

    def get_profiles(self, tenant_id: int, force: bool = False) -> List[Dict[str, Any]]:
        """Counselor profiles for this tenant — thread-safe with caching"""
        if not force:
            cached = self._tenant_cache.get(tenant_id)
            if cached is not None:
                return cached
        with self._cache_lock:
            # double-check after acquiring the lock
            if not force:
                cached = self._tenant_cache.get(tenant_id)
                if cached is not None:
                    return cached
            profiles = self._load_tenant_profiles(tenant_id)
            self._tenant_cache[tenant_id] = profiles
            return profiles

    def _load_tenant_profiles(self, tenant_id: int) -> List[Dict[str, Any]]:
        """This tenant's counselor profiles from the database (fallback: JSON only for tenant 1)"""
        try:
            conn = sqlite3.connect(str(USER_RECORDS_DB))
            cur = conn.cursor()
            cols = [c[1] for c in cur.execute("PRAGMA table_info(consultants)").fetchall()]
            if "tenant_id" not in cols:
                conn.close()
                return self._load_and_index_profiles()
            rows = cur.execute(
                "SELECT name, ability, location, education_experience, general_area, general_area_2, specializations, detailed_topics, age_range, license, notes FROM consultants WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
            conn.close()
            if not rows and tenant_id == 1:
                return self._load_and_index_profiles()
        except Exception:
            return self._load_and_index_profiles()
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
                "active_specs": specs_list
            })
        return indexed

    def match(self, user_info: Dict[str, Any], tenant_id: int = 1) -> List[Dict[str, Any]]:
        user_age = int(user_info.get("age", 0))
        user_gender_pref = user_info.get("preferred_gender", "فرقی ندارد")
        user_branch = user_info.get("branch", "اهمیتی ندارد")
        user_location_pref = user_info.get("location", "") or user_branch
        user_topic = (user_info.get("topic", "") + " " + user_info.get("expectation", "")).lower()
        ghq = user_info.get("ghq_scores")

        triggered_concepts = set()
        for concept, keywords in EXPANDED_SYNONYMS.items():
            if any(kw in user_topic for kw in keywords):
                triggered_concepts.add(concept)

        is_legal_request = "حقوقی" in triggered_concepts
        is_medical_request = "پزشکی_روانپزشکی" in triggered_concepts

        # Profiles of the same tenant — thread-safe (no global state)
        profiles = self.get_profiles(tenant_id)

        # Pass 1: hard filters
        branch_filter_active = user_branch in ["ظفر", "خیابان ایران"]

        def _passes_hard_filters(p) -> bool:
            if user_gender_pref in ["خانم", "زن"] and p["gender"] != "خانم":
                return False
            if user_gender_pref in ["آقا", "مرد"] and p["gender"] != "آقا":
                return False
            # Strict branch filter (like gender): only counselors working in the selected branch
            if branch_filter_active:
                loc = str(p.get("location", "")).strip()
                if loc != "هر دو" and user_branch not in loc:
                    return False
            if user_age > 0:
                if not (p["age_min"] - 1 <= user_age <= p["age_max"] + 1):
                    return False
            if not is_legal_request and ("وکالت" in p.get("education_experience", "") or p.get("active_specs") == ["مشاوره حقوقی خانواده"]):
                return False
            return True

        candidates = [p for p in profiles if _passes_hard_filters(p)]

        # Fallback pass 1: if the age filter finds nobody, drop age but keep gender/branch
        if not candidates:
            def _relax_age(p) -> bool:
                if user_gender_pref in ["خانم", "زن"] and p["gender"] != "خانم":
                    return False
                if user_gender_pref in ["آقا", "مرد"] and p["gender"] != "آقا":
                    return False
                if branch_filter_active:
                    loc = str(p.get("location", "")).strip()
                    if loc != "هر دو" and user_branch not in loc:
                        return False
                if not is_legal_request and ("وکالت" in p.get("education_experience", "") or p.get("active_specs") == ["مشاوره حقوقی خانواده"]):
                    return False
                return True
            candidates = [p for p in profiles if _relax_age(p)]

        # Fallback pass 2 (gender only): last line of defense so the user always gets an answer
        if not candidates:
            candidates = [
                p for p in profiles
                if not (
                    (user_gender_pref in ["خانم", "زن"] and p["gender"] != "خانم") or
                    (user_gender_pref in ["آقا", "مرد"] and p["gender"] != "آقا")
                )
                and not (not is_legal_request and ("وکالت" in p.get("education_experience", "") or p.get("active_specs") == ["مشاوره حقوقی خانواده"]))
            ]

        # Pass 2: clinical analysis and conceptual matching of the problem statement
        ghq_active = bool(ghq and isinstance(ghq, dict) and "total" in ghq)
        depression = ghq.get("depression", 0) if ghq_active else 0
        anxiety = ghq.get("anxiety", 0) if ghq_active else 0
        total_ghq = ghq.get("total", 0) if ghq_active else 0
        is_high_risk = ghq_active and (total_ghq >= 43 or depression >= 15)

        user_words = [w for w in re.findall(r'\w+', user_topic) if len(w) > 2]

        # Free appointments and learning weights are read once
        free_idx = _index_free_by_name(load_free_appointments())
        learning_w = load_learning_weights(tenant_id)

        scored_list = []
        for p in candidates:
            text = p["search_text"]
            score = 0.0
            reasons = []

            for w in user_words:
                if w in text:
                    score += 12.0

            if "جنس مخالف" in user_topic and "جنس مخالف" in text:
                score += 50.0
                reasons.append("تخصص ویژه در روابط عاطفی و ارتباط با جنس مخالف")
                
            if any(w in user_topic for w in ["جنسی", "سکس", "سکستراپی", "واژینیسموس", "زناشویی"]) and any(w in text for w in ["جنسی", "سکس", "سکستراپی", "مامایی"]):
                score += 45.0
                reasons.append("تخصص در سلامت جنسی و مشکلات زناشویی")
                
            if any(w in user_topic for w in ["کنکور", "انتخاب رشته", "تحصیلی", "برنامه ریزی"]) and any(w in text for w in ["کنکور", "انتخاب رشته", "تحصیلی", "برنامه ریزی"]):
                score += 45.0
                reasons.append("مشاوره تخصصی تحصیلی، کنکور و هدایت شغلی")
                
            if any(w in user_topic for w in ["یادگیری", "املا", "ریاضی", "دیکته", "نقص توجه", "بیش فعالی", "adhd"]) and any(w in text for w in ["یادگیری", "نقص توجه", "بیش فعالی", "adhd"]):
                score += 45.0
                reasons.append("درمان تخصصی اختلالات یادگیری و بیش‌فعالی (ADHD)")

            if "حقوقی" in triggered_concepts:
                if any(w in text for w in ["وکالت", "حقوقی", "دادگاه", "حقوق خانواده"]):
                    score += 80.0
                    reasons.append("مشاوره تخصصی حقوقی خانواده")

            if "پزشکی_روانپزشکی" in triggered_concepts:
                if any(w in text for w in ["روان پزشکان", "اعصاب و روان", "دارو", "طب ایرانی"]):
                    score += 80.0
                    reasons.append("متخصص اعصاب و روان (روانپزشک)")

            if "والد_فرزند" in triggered_concepts:
                if any(w in text for w in ["والد", "فرزند", "والدگری", "خانواده"]):
                    score += 50.0
                    reasons.append("تخصص در بهبود روابط و تعارضات والد و فرزند")

            if "ارتباط_تعارض" in triggered_concepts:
                if any(w in text for w in ["بین‌فردی", "بین فردی", "روابط", "ارتباط", "تعارض", "مهارت"]):
                    score += 45.0
                    reasons.append("تخصص در مهارت‌های ارتباطی و حل تعارضات بین‌فردی")

            if "نوجوان_جوان" in triggered_concepts or (12 <= user_age <= 19):
                if any(w in text for w in ["نوجوان", "نوجوانی", "جوان"]):
                    score += 35.0
                    reasons.append("مشاوره تخصصی رده سنی نوجوان و جوان")

            if "کودک" in triggered_concepts or (0 < user_age < 12):
                if any(w in text for w in ["کودک", "کودکان", "فرزندپروری", "بازی"]):
                    score += 40.0
                    reasons.append("تخصص در روانشناسی کودک و فرزندپروری")

            if "زوج_ازدواج" in triggered_concepts:
                if any(w in text for w in ["زوج", "ازدواج", "زناشویی", "همسر"]):
                    score += 45.0
                    reasons.append("تخصص در زوج‌درمانی و مشاوره پیش از ازدواج")

            if "افسردگی" in triggered_concepts:
                if any(w in text for w in ["افسردگی", "خلق", "خلقی", "سوگ", "طرحواره"]):
                    score += 40.0
                    reasons.append("درمان تخصصی افسردگی و افت انگیزه")

            if "وسواس" in triggered_concepts:
                if any(w in text for w in ["وسواس", "وسواسی", "ocd"]):
                    score += 45.0
                    reasons.append("درمان تخصصی وسواس فکری و عملی (OCD)")

            if "اضطراب" in triggered_concepts:
                if any(w in text for w in ["اضطراب", "استرس", "پانیک", "فوبیا"]):
                    score += 40.0
                    reasons.append("درمان تخصصی اضطراب و استرس")

            if "توسعه_فردی" in triggered_concepts:
                if any(w in text for w in ["توسعه فردی", "عزت نفس", "کوچینگ", "کمال"]):
                    score += 35.0
                    reasons.append("مشاوره توسعه فردی و عزت‌نفس")

            if ghq_active:
                if is_high_risk and ("روان پزشکان" in text or "اعصاب و روان" in text):
                    score += 60.0
                    reasons.append("ارزیابی بالینی و روانپزشکی بر اساس غربالگری GHQ")
                elif is_high_risk and ("بالینی" in text or "رواندرمانگر" in text):
                    score += 35.0
                    reasons.append("ارزیابی روان‌شناختی تخصصی بر اساس غربالگری GHQ")
                
                if depression >= 12 and ("افسردگی" in text or "خلق" in text):
                    score += depression * 1.5
                if anxiety >= 12 and ("اضطراب" in text or "وسواس" in text):
                    score += anxiety * 1.5

            score += float(p.get("ability", 2.0)) * 6.0

            free_slots = _count_free_for_indexed(p["clean_name"], user_location_pref, free_idx)
            if free_slots > 0:
                slot_score = min(free_slots * 3.0, 20.0)
                score += slot_score
                # No number: the exact count is visible only in the admin panel, not to the user
                reasons.append("نوبت آزاد در دسترس")

            # Concept-level self-taught weight — from the preloaded cache
            concept_boost = 1.0
            for concept in triggered_concepts:
                w = learning_w.get((p["clean_name"], concept), 1.0)
                if w != 1.0:
                    concept_boost *= w
            final_score = score * concept_boost

            clean_reasons = []
            for r in reasons:
                if r not in clean_reasons:
                    clean_reasons.append(r)

            reason_str = "، ".join(clean_reasons[:2]) if clean_reasons else "تطابق با رده سنی، موضوع مراجع و تخصص‌های مرکز"

            scored_list.append({
                "name": p["clean_name"],
                "score": round(final_score, 2),
                "ability": p["ability"],
                "free_slots": free_slots,
                "gender": p["gender"],
                "location": p["location"],
                "reason": reason_str
            })

        scored_list.sort(key=lambda x: x["score"], reverse=True)
        return scored_list[:2]

# Global engine instance — initialized at lifespan startup
engine = SpiralMatchEngine()
