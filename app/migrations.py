# -*- coding: utf-8 -*-
"""Migration های شماره‌دار با schema_version — فقط برای تغییرات جدید (تصمیم Q29).
migration های قدیمی ad-hoc دست‌نخورده می‌مانند."""
from app.config import USER_RECORDS_DB
from app.db import get_conn


def _mig_001_plans_quotas(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'weak',
            model TEXT NOT NULL DEFAULT '',
            base_url TEXT NOT NULL DEFAULT '',
            api_key_encrypted TEXT NOT NULL DEFAULT '',
            message_quota INTEGER NOT NULL DEFAULT -1,
            introduction_quota INTEGER NOT NULL DEFAULT -1,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tenant_plans (
            tenant_id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            messages_used INTEGER NOT NULL DEFAULT 0,
            introductions_used INTEGER NOT NULL DEFAULT 0,
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            assigned_by TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            plan_id INTEGER,
            previous_plan_id INTEGER,
            messages_used_at_change INTEGER NOT NULL DEFAULT 0,
            introductions_used_at_change INTEGER NOT NULL DEFAULT 0,
            changed_by TEXT,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS few_shot_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER,
            example_type TEXT NOT NULL DEFAULT 'match',
            prompt_text TEXT NOT NULL DEFAULT '',
            corrected_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_by TEXT,
            reviewed_at TIMESTAMP
        )
    """)
    # متن قفل per-tenant (Q47)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(tenants)").fetchall()]
    if cols and "lock_message" not in cols:
        conn.execute("ALTER TABLE tenants ADD COLUMN lock_message TEXT NOT NULL DEFAULT ''")


def _mig_002_for_other_step(conn):
    """استپ «برای خودت یا دیگری؟» به فلوهای موجود اضافه می‌شود (Q23: پیش‌فرض روشن)"""
    import json
    rows = conn.execute("SELECT id, flow_config FROM tenants").fetchall()
    rel_step = {
        "key": "relationship", "type": "choice", "source": "relationship",
        "label": "این اطلاعات برای چه کسی وارد می‌شود؟ (خودتان یا شخص دیگری)",
        "options": ["برای خودم", "برای فرزندم", "برای والدینم", "برای همسرم",
                    "برای خواهر یا برادرم", "برای دوستم", "برای سایر فامیل", "برای شخص دیگری"],
    }
    for tid, raw in rows:
        try:
            flow = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(flow, dict) or not flow.get("steps"):
            continue
        steps = flow["steps"]
        if steps and isinstance(steps[0], dict) and steps[0].get("key") == "relationship":
            flow["for_other"] = True
        elif flow.get("for_other") is False:
            pass  # ادمین صریح خاموش کرده
        else:
            flow["steps"] = [rel_step] + list(steps)
            flow["for_other"] = True
        conn.execute(
            "UPDATE tenants SET flow_config=? WHERE id=?",
            (json.dumps(flow, ensure_ascii=False), tid),
        )


def _mig_003_welcome_matches_relationship(conn):
    """پیام خوش‌آمد نباید «نام را وارد کنید» بگوید وقتی اولین سؤال، رابطه است (رفع باگ UX)"""
    import json
    rows = conn.execute("SELECT id, flow_config FROM tenants").fetchall()
    for tid, raw in rows:
        try:
            flow = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(flow, dict):
            continue
        steps = flow.get("steps") or []
        if not (steps and isinstance(steps[0], dict) and steps[0].get("key") == "relationship"):
            continue
        welcome = flow.get("welcome") or ""
        if "نام و نام خانوادگی" in welcome and "وارد فرمایید" in welcome:
            welcome = welcome.replace(
                "برای شروع، لطفاً **نام و نام خانوادگی** خود را وارد فرمایید:",
                "برای شروع، لطفاً به چند سؤال کوتاه زیر پاسخ دهید:",
            ).replace(
                "برای شروع، لطفاً نام و نام خانوادگی خود را وارد فرمایید:",
                "برای شروع، لطفاً به چند سؤال کوتاه زیر پاسخ دهید:",
            )
            flow["welcome"] = welcome
            conn.execute(
                "UPDATE tenants SET flow_config=? WHERE id=?",
                (json.dumps(flow, ensure_ascii=False), tid),
            )


def _mig_004_plan_history_month_key(conn):
    """ستون کلید ماهانه روی plan_history + ستون «آخرین ریست ماه» روی tenant_plans.

    «/ ماه» در جدول قیمت یعنی ریست خودکار ماهانه، نه «یک‌بار برای همیشه».
    اینکه «این tenant در این ماه ریست شده» باید از خودِ tenant_plans خوانده شود،
    نه از plan_history — چون plan_history هم تخصیص پلن را ثبت می‌کند و آن
    نباید جلوی ریست ماه بعد را بگیرد."""
    cols = [c[1] for c in conn.execute("PRAGMA table_info(plan_history)").fetchall()]
    if cols and "month_key" not in cols:
        conn.execute("ALTER TABLE plan_history ADD COLUMN month_key TEXT NOT NULL DEFAULT ''")
    cols = [c[1] for c in conn.execute("PRAGMA table_info(tenant_plans)").fetchall()]
    if cols and "last_reset_month" not in cols:
        conn.execute(
            "ALTER TABLE tenant_plans ADD COLUMN last_reset_month TEXT NOT NULL DEFAULT ''")


def _mig_005_plan_tier(conn):
    """ستون «ردهٔ پلن» روی plans — تا «کامل» از روی خودِ پلن شناخته شود.

    تا این مهاجرت، «کامل» این‌طور تشخیص داده می‌شد: mode=advanced و
    message_quota=-1. یعنی یک اشتباه تایپی سوپرادمین در فرم سقف، گزارشِ
    خریده‌شدهٔ مشتری را خاموش می‌کرد. حالا خودِ پلن می‌گوید کدام رده است."""
    cols = [c[1] for c in conn.execute("PRAGMA table_info(plans)").fetchall()]
    if cols and "tier" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN tier TEXT NOT NULL DEFAULT ''")


def _mig_006_seed_plan_tiers(conn):
    """برچسب ردهٔ پلن‌های موجود — از ترکیب فعلیِ mode و سقف، بدون دست‌زدن
    به مصرف یا پلن کسی."""
    for plan_id, mode, msg_q in conn.execute(
        "SELECT id, mode, message_quota FROM plans WHERE tier=''"
    ).fetchall():
        tier = {"weak": "base", "medium": "standard"}.get(mode)
        if not tier and mode == "advanced":
            tier = "full" if msg_q == -1 else "pro"
        if tier:
            conn.execute("UPDATE plans SET tier=? WHERE id=?", (tier, plan_id))


def _mig_007_tenant_quota_override(conn):
    """سقفِ اختصاصیِ هر مشتری + اعتبارِ بسته‌های اضافه، روی tenant_plans.

    تا این مهاجرت سقف فقط روی خودِ پلن بود: برای سقف متفاوت باید یک ردیف پلن
    تازه ساخت می‌شد، و هر ویرایش آن پلن همهٔ مشتری‌های رویش را retroactively
    عوض می‌کرد. «سقف کاملاً سفارشی» در جدول قیمت بدون این ستون‌ها دروغ بود.

    NULL در ستون override یعنی «از پلن ارث ببر»؛ extra_* اعتبارِ خریداری‌شده
    است که روی سقف مؤثر جمع می‌شود."""
    cols = [c[1] for c in conn.execute("PRAGMA table_info(tenant_plans)").fetchall()]
    if not cols:
        return
    for name, ddl in (
        ("message_quota_override", "INTEGER"),
        ("introduction_quota_override", "INTEGER"),
        ("extra_messages", "INTEGER NOT NULL DEFAULT 0"),
        ("extra_introductions", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tenant_plans ADD COLUMN {name} {ddl}")


def _mig_008_topup_packages(conn):
    """کاتالوگ بستهٔ اضافه — سوپرادمین تعریف می‌کند، مشتری می‌بیند و می‌خرد."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topup_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            extra_messages INTEGER NOT NULL DEFAULT 0,
            extra_introductions INTEGER NOT NULL DEFAULT 0,
            price_rials INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _mig_009_topup_requests(conn):
    """درخواست خریدِ بسته. مبلغ/تعداد همان لحظهٔ درخواست اسنپ‌شات می‌شود تا
    ویرایش بعدیِ بسته، بدهیِ مشتری را جابه‌جا نکند."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topup_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            package_id INTEGER,
            package_name TEXT NOT NULL DEFAULT '',
            extra_messages INTEGER NOT NULL DEFAULT 0,
            extra_introductions INTEGER NOT NULL DEFAULT 0,
            price_rials INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_by TEXT,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_by TEXT,
            resolved_at TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_topup_req_status ON topup_requests(status)")


def _mig_010_tenant_llm(conn):
    """پیکربندی LLM روی خودِ مشتری — نه روی پلن.

    تا این مهاجرت، base_url/model/api_key روی جدول plans بود و همهٔ مشتری‌های
    یک پلن ناچار یک کلید و یک مدل داشتند. حالا هر مشتری درگاه، کلید و مدل خودش
    را دارد؛ خالی بودن = ارث از پلن (سازگاری با وضعیت موجود)."""
    cols = [c[1] for c in conn.execute("PRAGMA table_info(tenant_plans)").fetchall()]
    if not cols:
        return
    for name in ("llm_base_url", "llm_model", "llm_api_key_encrypted"):
        if name not in cols:
            conn.execute(f"ALTER TABLE tenant_plans ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def _mig_012_welcome_name_request_matches_relationship(conn):
    """خوش‌آمدی که برای استپِ اول (رابطه) نام می‌خواهد، کاربر را به اشتباه می‌اندازد.

    _mig_003 فقط دو رشتهٔ دارای پیشوند «برای شروع،» را می‌شناخت؛ قالبِ دیگر
    («لطفاً **نام و نام خانوادگی** خود را وارد فرمایید:» بدون آن پیشوند)
    رد نشد و ماند. سؤال اولِ فلو «برای چه کسی» است، نه نام."""
    import json
    rows = conn.execute("SELECT id, flow_config FROM tenants").fetchall()
    invite = "لطفاً به چند سؤال کوتاه زیر پاسخ دهید:"
    for tid, raw in rows:
        try:
            flow = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(flow, dict):
            continue
        steps = flow.get("steps") or []
        if not (steps and isinstance(steps[0], dict) and steps[0].get("key") == "relationship"):
            continue
        welcome = flow.get("welcome") or ""
        if "نام و نام خانوادگی" not in welcome or "وارد فرمایید" not in welcome:
            continue
        if invite in welcome:
            continue
        # طولانی‌تر اول، تا «لطفاً **نام...**» قبل از نسخهٔ بدون ** جایگزین شود
        welcome = welcome.replace(
            "لطفاً **نام و نام خانوادگی** خود را وارد فرمایید:", invite,
        ).replace(
            "لطفاً نام و نام خانوادگی خود را وارد فرمایید:", invite,
        )
        flow["welcome"] = welcome
        conn.execute(
            "UPDATE tenants SET flow_config=? WHERE id=?",
            (json.dumps(flow, ensure_ascii=False), tid),
        )


def _mig_011_welcome_drop_repeated_question_invite(conn):
    """وقتی خوش‌آمد و پرامپتِ اولین استپ پشت‌سرهم می‌آیند، دعوت به سؤال دوبار تکرار می‌شود.

    «برای شروع، لطفاً به چند سؤال کوتاه زیر پاسخ دهید:» هم در انتهای خوش‌آمد
    هست و هم ابتدای برچسبِ استپ اول — کاربر دو بار یک جمله را می‌خواند.
    این مهاجرت جمله را از خوش‌آمدِ ذخیره‌شده برمی‌دارد تا فقط خودِ سؤال بماند."""
    import json
    rows = conn.execute("SELECT id, flow_config FROM tenants").fetchall()
    for tid, raw in rows:
        try:
            flow = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(flow, dict):
            continue
        welcome = flow.get("welcome") or ""
        invite = "برای شروع، لطفاً به چند سؤال کوتاه زیر پاسخ دهید:"
        if invite not in welcome:
            continue
        # جمله + فاصلهٔ قبلش را حذف کن تا welcome از جملهٔ خودش خالی نماند
        flow["welcome"] = welcome.replace(invite + "\n\n", "").replace(invite, "").rstrip()
        conn.execute(
            "UPDATE tenants SET flow_config=? WHERE id=?",
            (json.dumps(flow, ensure_ascii=False), tid),
        )


def _mig_013_remove_expectation_step(conn):
    """سؤال «انتظار از مشاور» کلا حذف شد.

    این سؤال عملاً چیزی به موتور مچینگ اضافه نمی‌کرد (فقط کنار topic در
    user_topic حلقه می‌شد) ولی گفتگو را طولانی و خشک می‌کرد — کاربر باید یک
    سؤال بی‌ربط به مشکلش را جواب می‌داد تا به معرفی برسد. در گفتگوی AI محور،
    AI خودش از دلِ حرف‌های مراجع انتظار او را درمی‌آورد."""
    import json
    rows = conn.execute("SELECT id, flow_config FROM tenants").fetchall()
    for tid, raw in rows:
        try:
            flow = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(flow, dict):
            continue
        steps = flow.get("steps")
        if not isinstance(steps, list):
            continue
        kept = [s for s in steps
                if not (isinstance(s, dict) and s.get("key") == "expectation")]
        if len(kept) == len(steps):
            continue
        flow["steps"] = kept
        conn.execute("UPDATE tenants SET flow_config=? WHERE id=?",
                     (json.dumps(flow, ensure_ascii=False), tid))


def _mig_014_only_primary_unlocked(conn):
    """سیاست: هیچ مشتری‌ای پلنِ پیش‌فرض ندارد — فقط tenant نمونه فعال می‌ماند.

    مشتری تازه هم از نو بدون پلن ساخته می‌شود (create_tenant هیچ ردیفی در
    tenant_plans نمی‌زند)؛ این مهاجرت فقط وضعیت موجود را به همان سیاست
    می‌رساند. plan_history دست‌نخورده می‌ماند تا audit پابرجا باشد."""
    row = conn.execute("SELECT id FROM tenants WHERE slug='demo'").fetchone()
    if not row:
        # دیتابیس تازه (seed هنوز نخورده) — «NOT IN زیرپرسشِ خالی» همه را حذف می‌کند!
        return
    conn.execute("DELETE FROM tenant_plans WHERE tenant_id != ?", (row[0],))


def _mig_015_spouse_relation_step(conn):
    """سؤال شرطی «کیفیت ارتباط شما با همسرتان» فقط برای «برای فرزندم».

    والدی که برای فرزندش فرم پر می‌کند باید یک سؤال خانوادگی هم بپرسد؛
    پاسخ میانه/پایین در پیام پایانی پیشنهاد زوج‌درمانگر می‌آورد.
    فقط در فلوهایی که استپ relationship دارند درج می‌شود (شرط باید
    بتواند برقرار شود) و اگر از قبل هست، دست نمی‌خورد."""
    import json
    step = {
        "key": "spouse_relation", "type": "choice",
        "conditional": "relationship=برای فرزندم",
        "options": ["خیلی خوب", "میانه", "پایین"],
        "label": "کیفیت ارتباط شما با همسرتان چگونه است؟",
        "source": "spouse_relation",
    }
    rows = conn.execute("SELECT id, flow_config FROM tenants").fetchall()
    for tid, raw in rows:
        try:
            flow = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(flow, dict):
            continue
        steps = flow.get("steps")
        if not isinstance(steps, list):
            continue
        if any(isinstance(s, dict) and s.get("key") == "spouse_relation" for s in steps):
            continue
        rel_idx = next(
            (i for i, s in enumerate(steps)
             if isinstance(s, dict) and s.get("key") == "relationship"),
            None,
        )
        if rel_idx is None:
            continue
        flow["steps"] = steps[:rel_idx + 1] + [dict(step)] + steps[rel_idx + 1:]
        conn.execute("UPDATE tenants SET flow_config=? WHERE id=?",
                     (json.dumps(flow, ensure_ascii=False), tid))


_MIGRATIONS = [
    (1, _mig_001_plans_quotas),
    (2, _mig_002_for_other_step),
    (3, _mig_003_welcome_matches_relationship),
    (4, _mig_004_plan_history_month_key),
    (5, _mig_005_plan_tier),
    (6, _mig_006_seed_plan_tiers),
    (7, _mig_007_tenant_quota_override),
    (8, _mig_008_topup_packages),
    (9, _mig_009_topup_requests),
    (10, _mig_010_tenant_llm),
    (11, _mig_011_welcome_drop_repeated_question_invite),
    (12, _mig_012_welcome_name_request_matches_relationship),
    (13, _mig_013_remove_expectation_step),
    (14, _mig_014_only_primary_unlocked),
    (15, _mig_015_spouse_relation_step),
]


def run_migrations():
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
        current = row[0] if row else 0
        for version, fn in _MIGRATIONS:
            if version > current:
                fn(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version(version) VALUES (?)", (version,)
                )
        conn.commit()
    finally:
        conn.close()
