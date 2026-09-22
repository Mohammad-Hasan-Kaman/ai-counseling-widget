# -*- coding: utf-8 -*-
"""
ماژول مدیریت دیتابیس مراجعین و ثبت سوابق مشاوره‌ها — چندمشتری (per-tenant)
هویت هر نفر = شماره + نام نرمال‌شده؛ شمارهٔ تکراری با نام دیگر دیگر کاربر را
بلاک نمی‌کند، بلکه پروفایل جدا با وضعیت «در انتظار بررسی» می‌سازد تا ادمین
در صفحهٔ تعارض‌ها تصمیم بگیرد (ادغام / هر دو واقعی / بایگانی).
مرکز مشاوره خانواده نیک‌روان
"""
import sqlite3
import json
import re
from pathlib import Path
from app.config import USER_RECORDS_DB
from app.db import get_conn

# ── نرمال‌سازی و تطبیق نام ──

def normalize_name(name: str) -> str:
    """حذف فاصله/نیم‌فاصله/علائم و یکدست‌سازی حروف برای مقایسهٔ نام‌ها"""
    s = str(name or "").strip().lower()
    s = s.replace("ي", "ی").replace("ك", "ک").replace("‌", "").replace(" ", "")
    s = re.sub(r"[.\-_]", "", s)
    return s


def names_similar(a: str, b: str) -> bool:
    """تطبیق فازی نام‌های یک شماره: اختلاف تایپی/فاصله نباید پروفایل دوم بسازد"""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    # فاصلهٔ ویرایشی حداکثر ۲ برای نام‌های بلند (تایپ اشتباه رایج)
    if len(na) >= 6 and len(nb) >= 6:
        la, lb = na[:24], nb[:24]
        d = _levenshtein(la, lb)
        return d <= 2
    return False


def _levenshtein(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 3:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[len(b)]


def is_suspicious_phone(phone: str) -> bool:
    """شماره‌های واضحاً ساختگی: پیش‌شماره 090 یا ارقام تکراری/دنباله‌دار"""
    p = str(phone or "")
    if len(p) != 11 or not p.isdigit():
        return False
    if p.startswith("090"):
        return True
    tail = p[3:]
    if len(set(tail)) <= 1:
        return True
    digits = [int(x) for x in tail]
    if all(digits[i + 1] - digits[i] == 1 for i in range(len(digits) - 1)):
        return True
    return False


def init_user_db():
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()

    # جدول مشخصات مراجعین — کلید مرکب (tenant_id, phone, name_norm)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        tenant_id INTEGER NOT NULL DEFAULT 1,
        phone TEXT NOT NULL,
        full_name TEXT NOT NULL,
        name_norm TEXT NOT NULL DEFAULT '',
        first_telegram_id INTEGER,
        request_count INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tenant_id, phone, name_norm)
    )
    """)

    # جدول سوابق درخواست‌ها و نتایج
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER DEFAULT 1,
        telegram_id INTEGER,
        phone TEXT,
        full_name TEXT,
        gender TEXT,
        age INTEGER,
        topic TEXT,
        has_prev_therapy INTEGER,
        prev_detail TEXT,
        expectation TEXT,
        preferred_gender TEXT,
        branch TEXT,
        ghq_scores TEXT,
        ghq_total INTEGER,
        recommendations TEXT,
        request_number INTEGER,
        session_token TEXT,
        custom_data TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()

    # مایگریشن جداول قدیمی: افزودن ستون‌های نبود + مهاجرت کلید مرکب
    existing_cols = [c[1] for c in cur.execute("PRAGMA table_info(user_requests)").fetchall()]
    for col, decl in [("branch", "TEXT"), ("session_token", "TEXT"), ("tenant_id", "INTEGER DEFAULT 1"), ("custom_data", "TEXT")]:
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE user_requests ADD COLUMN {col} {decl}")

    u_cols = [c[1] for c in cur.execute("PRAGMA table_info(users)").fetchall()]
    pk_order = sorted([(c[5], c[1]) for c in cur.execute("PRAGMA table_info(users)").fetchall() if c[5]])
    pk_names = [name for _, name in pk_order]
    if pk_names != ["tenant_id", "phone", "name_norm"]:
        # ساختار قدیمی (phone یا (tenant_id, phone) تنها PK) → مهاجرت به (tenant_id, phone, name_norm)
        cur.execute("""
            CREATE TABLE users_new (
                tenant_id INTEGER NOT NULL DEFAULT 1,
                phone TEXT NOT NULL,
                full_name TEXT NOT NULL,
                name_norm TEXT NOT NULL DEFAULT '',
                first_telegram_id INTEGER,
                request_count INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, phone, name_norm)
            )
        """)
        has_tenant = "tenant_id" in u_cols
        has_count = "request_count" in u_cols
        sel_tenant = "tenant_id" if has_tenant else "1"
        sel_count = "COALESCE(request_count, 0)" if has_count else "0"
        rows = cur.execute(f"""
            SELECT {sel_tenant}, phone, full_name, first_telegram_id, {sel_count}, created_at, last_seen
            FROM users
        """).fetchall()
        merged = {}
        for (tid, phone, fname, tg, cnt, created, seen) in rows:
            key = (tid, phone, normalize_name(fname or ""))
            if key in merged:
                old = merged[key]
                if (cnt or 0) > (old[4] or 0):
                    merged[key] = (tid, phone, fname, tg, cnt, created, seen)
            else:
                merged[key] = (tid, phone, fname, tg, cnt, created, seen)
        for (tid, phone, fname, tg, cnt, created, seen) in merged.values():
            cur.execute("""
                INSERT OR IGNORE INTO users_new (tenant_id, phone, full_name, name_norm, first_telegram_id, request_count, status, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """, (tid, phone, fname or "—", normalize_name(fname or ""), tg, cnt, created, seen))
        cur.execute("DROP TABLE users")
        cur.execute("ALTER TABLE users_new RENAME TO users")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant_phone ON users(tenant_id, phone)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_tenant ON user_requests(tenant_id)")
    conn.commit()
    conn.close()


def init_consultants_db():
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS consultants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER DEFAULT 1,
        name TEXT NOT NULL,
        ability REAL DEFAULT 2.0,
        location TEXT DEFAULT 'هر دو',
        education_experience TEXT,
        general_area TEXT,
        general_area_2 TEXT,
        specializations TEXT,
        detailed_topics TEXT,
        age_range TEXT,
        license TEXT,
        notes TEXT,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS upload_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER DEFAULT 1,
        consultant_count INTEGER,
        file_size_bytes INTEGER,
        uploaded_by INTEGER,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # مایگریشن: حذف UNIQUE قدیمی روی name با ساخت مجدد در صورت نیاز
    idx = cur.execute("PRAGMA index_list(consultants)").fetchall()
    has_unique_name = any(
        cur.execute(f"PRAGMA index_info({i[1]})").fetchall() and i[3] == 1 and
        [c[2] for c in cur.execute(f"PRAGMA index_info({i[1]})").fetchall()] == ["name"]
        for i in idx
    )
    if has_unique_name:
        cur.execute("""
            CREATE TABLE consultants_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER DEFAULT 1,
                name TEXT NOT NULL,
                ability REAL DEFAULT 2.0,
                location TEXT DEFAULT 'هر دو',
                education_experience TEXT,
                general_area TEXT,
                general_area_2 TEXT,
                specializations TEXT,
                detailed_topics TEXT,
                age_range TEXT,
                license TEXT,
                notes TEXT,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            INSERT INTO consultants_new (id, name, ability, location, education_experience, general_area,
                general_area_2, specializations, detailed_topics, age_range, license, notes, uploaded_at)
            SELECT id, name, ability, location, education_experience, general_area,
                general_area_2, specializations, detailed_topics, age_range, license, notes, uploaded_at FROM consultants
        """)
        cur.execute("DROP TABLE consultants")
        cur.execute("ALTER TABLE consultants_new RENAME TO consultants")
    cols = [c[1] for c in cur.execute("PRAGMA table_info(consultants)").fetchall()]
    if "tenant_id" not in cols:
        cur.execute("ALTER TABLE consultants ADD COLUMN tenant_id INTEGER DEFAULT 1")
    cols = [c[1] for c in cur.execute("PRAGMA table_info(upload_history)").fetchall()]
    if "tenant_id" not in cols:
        cur.execute("ALTER TABLE upload_history ADD COLUMN tenant_id INTEGER DEFAULT 1")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_consultants_tenant ON consultants(tenant_id)")
    conn.commit()
    conn.close()


def replace_consultants(profiles: list, uploaded_by: int = 0, tenant_id: int = 1) -> dict:
    """
    جایگزینی کامل دیتای مشاورین یک مشتری: داده قدیمی حذف و لیست جدید درج می‌شود.
    خروجی: آمار مقایسه‌ای با دیتای قبلی برای گزارش
    """
    import json as _json
    init_consultants_db()
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()
    try:
        old_names = {r[0] for r in cur.execute("SELECT name FROM consultants WHERE tenant_id=?", (tenant_id,)).fetchall()}
        old_count = len(old_names)

        cur.execute("DELETE FROM consultants WHERE tenant_id=?", (tenant_id,))
        inserted = 0
        for p in profiles:
            cur.execute("""
                INSERT OR REPLACE INTO consultants
                (tenant_id, name, ability, location, education_experience, general_area,
                 general_area_2, specializations, detailed_topics, age_range, license, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tenant_id,
                p.get("name", ""), p.get("ability", 2.0), p.get("location", "هر دو"),
                p.get("education_experience", ""), p.get("general_area", ""),
                p.get("general_area_2", ""), _json.dumps(p.get("specializations", {}), ensure_ascii=False),
                p.get("detailed_topics", ""), p.get("age_range", ""),
                p.get("license", ""), p.get("notes", ""),
            ))
            inserted += 1

        new_names = {p.get("name", "") for p in profiles}
        added = new_names - old_names
        removed = old_names - new_names

        cur.execute(
            "INSERT INTO upload_history (tenant_id, consultant_count, uploaded_by) VALUES (?, ?, ?)",
            (tenant_id, len(profiles), uploaded_by),
        )
        conn.commit()
        return {
            "old_count": old_count,
            "new_count": len(profiles),
            "inserted": inserted,
            "added": sorted(added),
            "removed": sorted(removed),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_consultants_stats(tenant_id: int = 1) -> dict:
    """آمار فیلدهای پرشده جدول مشاورین یک مشتری برای گزارش صحت داده"""
    import json as _json
    init_consultants_db()
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM consultants WHERE tenant_id=?", (tenant_id,)).fetchone()[0]

    def _filled(col):
        return cur.execute(
            f"SELECT COUNT(*) FROM consultants WHERE tenant_id=? AND {col} IS NOT NULL AND TRIM({col}) != ''",
            (tenant_id,),
        ).fetchone()[0]

    stats = {
        "total": total,
        "with_ability": _filled("ability"),
        "with_location": _filled("location"),
        "with_education": _filled("education_experience"),
        "with_general_area": _filled("general_area"),
        "with_age_range": _filled("age_range"),
        "with_license": _filled("license"),
        "with_notes": _filled("notes"),
        "with_specializations": 0,
        "spec_total": 0,
        "by_location": {},
    }

    for loc, cnt in cur.execute("SELECT location, COUNT(*) FROM consultants WHERE tenant_id=? GROUP BY location", (tenant_id,)):
        stats["by_location"][loc or "-"] = cnt

    for (spec_json,) in cur.execute("SELECT specializations FROM consultants WHERE tenant_id=?", (tenant_id,)).fetchall():
        try:
            spec = _json.loads(spec_json) if spec_json else {}
            if isinstance(spec, dict) and spec:
                stats["with_specializations"] += 1
                stats["spec_total"] += len(spec)
        except Exception:
            pass

    conn.close()
    return stats


def validate_phone_and_get_count(phone: str, input_name: str, tenant_id: int = 1) -> tuple[bool, int, str]:
    """
    محاسبه مرتبه مراجعه برای (شماره، نام). هیچ‌کس بلاک نمی‌شود؛
    خروجی: (مجاز_بودن=True همیشه, شماره_مراجعه, نام_تطبیق‌یافته)
    """
    init_user_db()
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT full_name, name_norm, request_count, status FROM users WHERE tenant_id=? AND phone=?",
        (tenant_id, phone),
    ).fetchall()
    conn.close()

    if not rows:
        return True, 1, input_name

    norm_input = normalize_name(input_name)
    # تطبیق دقیق نرمال‌شده یا فازی با یکی از پروفایل‌های همین شماره
    for full_name, name_norm, cnt, status in rows:
        if norm_input == name_norm or names_similar(input_name, full_name):
            return True, (cnt or 0) + 1, full_name
    # شماره مشترک — پروفایل جدید (در save ساخته می‌شود و به صف تعارض می‌رود)
    return True, 1, input_name


def save_user_consultation(session_token: str, user_data: dict, recommendations: list,
                           tenant_id: int = 1, custom_data: dict | None = None) -> int:
    """ثبت سابقه و ارتقای شمارنده مراجعات (ویجت وب — per-tenant)"""
    phone = user_data.get("phone", "")
    full_name = (user_data.get("full_name", "") or "—").strip()
    name_norm = normalize_name(full_name)

    init_user_db()
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()

    rows = cur.execute(
        "SELECT full_name, name_norm, request_count, status FROM users WHERE tenant_id=? AND phone=?",
        (tenant_id, phone),
    ).fetchall()

    req_number = 1
    matched = None
    for r_full, r_norm, r_cnt, r_status in rows:
        if name_norm == r_norm or names_similar(full_name, r_full):
            matched = (r_full, r_norm, r_cnt, r_status)
            break

    if matched:
        r_full, r_norm, r_cnt, r_status = matched
        req_number = (r_cnt or 0) + 1
        cur.execute(
            "UPDATE users SET request_count=?, last_seen=CURRENT_TIMESTAMP"
            " WHERE tenant_id=? AND phone=? AND name_norm=?",
            (req_number, tenant_id, phone, r_norm),
        )
        # اگر پروفایل قبلاً بایگانی شده بود و باز ثبت کرد، دوباره به بررسی برگردد
        if r_status == "archived":
            cur.execute(
                "UPDATE users SET status='pending' WHERE tenant_id=? AND phone=? AND name_norm=?",
                (tenant_id, phone, r_norm),
            )
        full_name = r_full  # نام تأییدشدهٔ همان پروفایل
    else:
        # پروفایل جدید: اگر شماره از قبل نام دیگری دارد → وضعیت pending + قدیمی‌ها هم pending (تعارض)
        first_profile = not rows
        status = "active" if first_profile else "pending"
        cur.execute("""
            INSERT INTO users (tenant_id, phone, full_name, name_norm, first_telegram_id, request_count, status)
            VALUES (?, ?, ?, ?, NULL, 1, ?)
        """, (tenant_id, phone, full_name, name_norm, status))
        if not first_profile:
            cur.execute(
                "UPDATE users SET status='pending' WHERE tenant_id=? AND phone=? AND status='active'",
                (tenant_id, phone),
            )

    ghq = user_data.get("ghq_scores")
    ghq_total = None
    if ghq and isinstance(ghq, dict):
        try:
            ghq_total = int(ghq.get("total")) if ghq.get("total") is not None else None
        except (TypeError, ValueError):
            ghq_total = None
    ghq_json = json.dumps(ghq, ensure_ascii=False) if ghq else None
    recs_json = json.dumps(recommendations, ensure_ascii=False)
    custom_json = json.dumps(custom_data, ensure_ascii=False) if custom_data else None

    cur.execute("""
    INSERT INTO user_requests (
        tenant_id, telegram_id, phone, full_name, gender, age, topic,
        has_prev_therapy, prev_detail, expectation, preferred_gender,
        branch, ghq_scores, ghq_total, recommendations, request_number, session_token, custom_data
    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        tenant_id, phone, full_name, user_data.get("gender"), user_data.get("age"),
        user_data.get("topic"), 1 if user_data.get("has_prev_therapy") else 0,
        user_data.get("prev_detail", ""), user_data.get("expectation", ""),
        user_data.get("preferred_gender", ""), user_data.get("branch", "اهمیتی ندارد"),
        ghq_json, ghq_total, recs_json, req_number, session_token, custom_json
    ))

    conn.commit()
    conn.close()
    return req_number


# ── مدیریت تعارض شماره‌ها (صف بررسی ادمین) ──

def list_phone_conflicts(tenant_id: int) -> list:
    """شماره‌هایی که بیش از یک پروفایل در انتظار بررسی دارند (فقط pending): [{phone, profiles:[...]}]"""
    init_user_db()
    conn = get_conn(USER_RECORDS_DB)
    phones = [r[0] for r in conn.execute(
        "SELECT phone FROM users WHERE tenant_id=? AND status='pending'"
        " GROUP BY phone HAVING COUNT(*) > 1 ORDER BY MAX(last_seen) DESC",
        (tenant_id,),
    ).fetchall()]
    out = []
    for phone in phones:
        profiles = []
        for (fname, norm, cnt, status, created, seen) in conn.execute(
            "SELECT full_name, name_norm, request_count, status, created_at, last_seen"
            " FROM users WHERE tenant_id=? AND phone=? AND status='pending'"
            " ORDER BY created_at ASC",
            (tenant_id, phone),
        ).fetchall():
            reqs = conn.execute(
                "SELECT ur.id, ur.topic, ur.created_at FROM user_requests ur"
                " JOIN users u ON u.tenant_id=ur.tenant_id AND u.phone=ur.phone"
                " AND u.full_name=ur.full_name"
                " WHERE ur.tenant_id=? AND ur.phone=? AND u.name_norm=?"
                " ORDER BY ur.created_at DESC LIMIT 3",
                (tenant_id, phone, norm),
            ).fetchall()
            profiles.append({
                "full_name": fname, "name_norm": norm, "request_count": cnt,
                "status": status, "created": created, "last_seen": seen,
                "recent_requests": reqs,
            })
        out.append({"phone": phone, "profiles": profiles})
    conn.close()
    return out


def resolve_phone_conflict(tenant_id: int, phone: str, keep_norm: str | None,
                           action: str) -> dict:
    """
    حل تعارض یک شماره.
    action:
      - "merge": همهٔ پروفایل‌های غیربایگانی در keep_norm ادغام می‌شوند (count جمع، درخواست‌ها به نام نگه‌داشته منتقل)
      - "both": همه فعال می‌شوند (گوشی مشترک) — از صف خارج
      - "archive": پروفایل‌های غیر از keep_norm بایگانی می‌شوند
    خروجی: {"merged_requests": n} یا {"archived": n} یا {"activated": n}
    """
    init_user_db()
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()
    try:
        result = {}
        if action == "merge":
            target = conn.execute(
                "SELECT name_norm, request_count FROM users"
                " WHERE tenant_id=? AND phone=? AND name_norm=?",
                (tenant_id, phone, keep_norm),
            ).fetchone()
            if not target:
                conn.rollback()
                return {"error": "پروفایل مقصد یافت نشد."}
            others = conn.execute(
                "SELECT name_norm, full_name FROM users"
                " WHERE tenant_id=? AND phone=? AND name_norm != ? AND status != 'archived'",
                (tenant_id, phone, keep_norm),
            ).fetchall()
            total = target[1] or 0
            moved = 0
            for (o_norm, o_full) in others:
                cnt_row = conn.execute(
                    "SELECT request_count FROM users WHERE tenant_id=? AND phone=? AND name_norm=?",
                    (tenant_id, phone, o_norm),
                ).fetchone()
                total += (cnt_row[0] if cnt_row else 0)
                # درخواست‌های پروفایل حذفی به نام نگه‌داشته منتقل می‌شوند
                cur2 = conn.execute(
                    "UPDATE user_requests SET full_name=(SELECT full_name FROM users"
                    " WHERE tenant_id=? AND phone=? AND name_norm=?)"
                    " WHERE tenant_id=? AND phone=? AND LOWER(TRIM(full_name))=?",
                    (tenant_id, phone, keep_norm, tenant_id, phone, o_full.strip().lower()),
                )
                moved += cur2.rowcount if cur2.rowcount and cur2.rowcount > 0 else 0
                cur.execute(
                    "DELETE FROM users WHERE tenant_id=? AND phone=? AND name_norm=?",
                    (tenant_id, phone, o_norm),
                )
            cur.execute(
                "UPDATE users SET request_count=?, status='active' WHERE tenant_id=? AND phone=? AND name_norm=?",
                (total, tenant_id, phone, keep_norm),
            )
            conn.commit()
            result = {"merged_requests": moved, "total_count": total}
        elif action == "both":
            actives = conn.execute(
                "SELECT COUNT(*) FROM users WHERE tenant_id=? AND phone=? AND status='active'",
                (tenant_id, phone),
            ).fetchone()[0]
            cur2 = conn.execute(
                "UPDATE users SET status='active' WHERE tenant_id=? AND phone=? AND status='pending'",
                (tenant_id, phone),
            )
            conn.commit()
            result = {"activated": (cur2.rowcount or 0) + actives}
        elif action == "archive":
            cur2 = conn.execute(
                "UPDATE users SET status='archived' WHERE tenant_id=? AND phone=?"
                " AND name_norm != ? AND status != 'archived'",
                (tenant_id, phone, keep_norm),
            )
            conn.commit()
            result = {"archived": cur2.rowcount}
        else:
            result = {"error": "عملیات نامعتبر."}
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_suspicious_phones(tenant_id: int) -> list:
    """شماره‌های ساختگیِ فعال — فقط اطلاع‌رسانی، بلاک نیست"""
    init_user_db()
    conn = get_conn(USER_RECORDS_DB)
    rows = conn.execute(
        "SELECT phone, full_name, request_count, last_seen FROM users"
        " WHERE tenant_id=? AND status='active' ORDER BY last_seen DESC",
        (tenant_id,),
    ).fetchall()
    conn.close()
    return [
        {"phone": p, "full_name": f, "request_count": c, "last_seen": s}
        for (p, f, c, s) in rows if is_suspicious_phone(p)
    ]
