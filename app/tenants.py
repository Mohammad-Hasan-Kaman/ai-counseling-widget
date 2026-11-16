# -*- coding: utf-8 -*-
"""مدیریت مشتریان (tenant): ساخت، کلید API، seed اولیه"""
import json
import secrets
import re

from app.config import USER_RECORDS_DB, ADMIN_PANEL_PASSWORD, SUPER_ADMIN_USERNAME, SUPER_ADMIN_PASSWORD
from app.db import get_conn
from app.auth import hash_password

DEFAULT_DEMO_FLOW = {
    "welcome": (
        "🌸 **سلام! به سامانه هوشمند پذیرش مرکز «مشاوره نمونه» خوش آمدید.**\n\n"
        "🤖 من دستیار هوشمند تریاژ هستم و کمکتان می‌کنم **بهترین مشاور این مرکز را برای خودتان انتخاب کنید.**\n"
        "کافی است به چند سؤال کوتاه پاسخ دهید تا بر اساس موضوع، شرایط و ترجیح‌های شما، مناسب‌ترین مشاوران را با دلیل معرفی کنم."
    ),
    "steps": [
        {"key": "relationship", "type": "choice", "source": "relationship",
         "label": "این اطلاعات برای چه کسی وارد می‌شود؟ (خودتان یا شخص دیگری)",
         "options": ["برای خودم", "برای فرزندم", "برای والدینم", "برای همسرم",
                     "برای خواهر یا برادرم", "برای دوستم", "برای سایر فامیل", "برای شخص دیگری"]},
        # فقط برای «برای فرزندم»: سؤال از والد — پاسخ میانه/پایین در پایان
        # پیشنهاد زوج‌درمانگر می‌آورد (_couple_advice در session_store)
        {"key": "spouse_relation", "type": "choice",
         "conditional": "relationship=برای فرزندم",
         "options": ["خیلی خوب", "میانه", "پایین"],
         "label": "کیفیت ارتباط شما با همسرتان چگونه است؟",
         "source": "spouse_relation"},
        {"key": "full_name", "label": None, "type": "text", "source": "full_name"},
        {"key": "phone", "type": "phone"},
        {"key": "gender", "type": "choice", "options": ["آقا", "خانم"], "source": "gender"},
        {"key": "age", "type": "number", "source": "age"},
        {"key": "topic", "type": "text", "label": "لطفاً موضوع اصلی یا مشکلی که برای آن به مشاوره نیاز دارید را بنویسید:", "source": "topic"},
        {"key": "prev_therapy", "type": "choice", "options": ["بله", "خیر"], "source": "has_prev_therapy", "label": "آیا سابقه مراجعه قبلی به روان‌شناس یا روان‌پزشک داشته‌اید؟"},
        {"key": "prev_detail", "type": "text", "conditional": "has_prev_therapy=بله", "label": "در صورت تمایل توضیح مختصری درباره درمان قبلی بنویسید (در غیر این صورت یک پیام کوتاه ارسال کنید):", "source": "prev_detail"},
        {"key": "preferred_gender", "type": "choice", "options": ["آقا", "خانم", "فرقی ندارد"], "label": "ترجیح می‌دهید جنسیت مشاور شما چه باشد؟", "source": "preferred_gender"},
        {"key": "branch", "type": "choice", "options": ["شعبه مرکزی", "شعبه شمال", "اهمیتی ندارد"], "label": "کدام شعبه مرکز مشاوره نمونه برای شما مناسب‌تر است؟", "source": "branch"},
    ],
    "ghq": True,
    "ending": "recommend",
    "for_other": True,
    "thanks_text": "متشکریم! اطلاعات شما ثبت شد و در اسرع وقت با شما تماس می‌گیریم.",
}


def new_api_key() -> str:
    return "nk_" + secrets.token_hex(16)


def slugify(name: str) -> str:
    s = re.sub(r"\s+", "-", name.strip()).strip("-")
    return s or "tenant"


def create_tenant(name: str, admin_username: str, admin_password: str,
                  allowed_domains: str = "") -> dict:
    """ساخت مشتری + ادمین اول آن. خروجی: {tenant_id, api_key}"""
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()
    base_slug = slugify(name)
    slug = base_slug
    i = 2
    while cur.execute("SELECT 1 FROM tenants WHERE slug=?", (slug,)).fetchone():
        slug = f"{base_slug}-{i}"
        i += 1
    api_key = new_api_key()
    cur.execute(
        "INSERT INTO tenants (name, slug, api_key, allowed_domains, active, flow_config) VALUES (?,?,?,?,1,?)",
        (name, slug, api_key, allowed_domains.strip(), json.dumps(default_public_flow(), ensure_ascii=False)),
    )
    tenant_id = cur.lastrowid
    cur.execute(
        "INSERT INTO panel_users (username, password_hash, role, tenant_id, active) VALUES (?,?,?,?,1)",
        (admin_username, hash_password(admin_password), "admin", tenant_id),
    )
    conn.commit()
    conn.close()
    return {"tenant_id": tenant_id, "api_key": api_key, "slug": slug}


def default_public_flow() -> dict:
    """فلوی پیش‌فرض برای مشتری‌های عمومی: سیگنال‌های موتور قاعده‌ای + GHQ + معرفی دو مشاور.

    بدون topic/سن/ترجیح جنسیت، موتور مچینگ عملاً کور است و «معرفی خودکار دو مشاور»
    که در همهٔ پلن‌ها تبلیغ شده از جعبه‌بیرون کار نمی‌کند (welcome هم همین را وعده
    می‌دهد). نام شعبه‌ها عمداً نیست — center-specific است و ادمین در ویرایشگر فلو
    اضافه‌اش می‌کند (بدون آن، شعبه «اهمیتی ندارد» است و فیلتری اعمال نمی‌شود)."""
    return {
        "welcome": (
            "🌸 **سلام! خوش آمدید.**\n\n"
            "🤖 من دستیار هوشمند تریاژ هستم و کمکتان می‌کنم **بهترین مشاور این مرکز را برای خودتان انتخاب کنید؛**\n"
            "پس از پاسخ به چند سؤال کوتاه، اطلاعات شما ثبت می‌شود و کارشناسان مرکز با شما تماس می‌گیرند.\n\n"
            "برای شروع، لطفاً به چند سؤال کوتاه زیر پاسخ دهید:"
        ),
        "steps": [
            {"key": "relationship", "type": "choice", "source": "relationship",
             "label": "این اطلاعات برای چه کسی وارد می‌شود؟ (خودتان یا شخص دیگری)",
             "options": ["برای خودم", "برای فرزندم", "برای والدینم", "برای همسرم",
                         "برای خواهر یا برادرم", "برای دوستم", "برای سایر فامیل", "برای شخص دیگری"]},
            {"key": "spouse_relation", "type": "choice",
             "conditional": "relationship=برای فرزندم",
             "options": ["خیلی خوب", "میانه", "پایین"],
             "label": "کیفیت ارتباط شما با همسرتان چگونه است؟",
             "source": "spouse_relation"},
            {"key": "full_name", "type": "text", "label": None, "source": "full_name"},
            {"key": "phone", "type": "phone"},
            {"key": "topic", "type": "text", "label": "مهم‌ترین چیزی که می‌خواهید دربارهٔ آن مشورت بگیرید چیست؟", "source": "topic"},
            {"key": "age", "type": "number", "label": "سن شما چند سال است؟", "source": "age"},
            {"key": "preferred_gender", "type": "choice", "label": "ترجیح می‌دهید مشاور شما چه جنسیتی باشد؟",
             "source": "preferred_gender", "options": ["آقا", "خانم", "فرقی ندارد"]},
        ],
        "ghq": True,
        "ending": "recommend",
        "for_other": True,
        "thanks_text": "متشکریم! اطلاعات شما ثبت شد و در اسرع وقت با شما تماس می‌گیریم.",
    }


def _parse_flow_config(raw: str | None) -> dict:
    """flow_config خراب یا پوچ → فلوی پیش‌فرض (به‌جای ۵۰۰)"""
    try:
        flow = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        flow = default_public_flow()
    if not isinstance(flow, dict) or not flow.get("steps"):
        flow = default_public_flow()
    return flow


def get_tenant_by_key(api_key: str) -> dict | None:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT id, name, slug, api_key, allowed_domains, active, flow_config FROM tenants WHERE api_key=?",
        (api_key,),
    ).fetchone()
    conn.close()
    if not row or not row[5]:
        return None
    return {
        "id": row[0], "name": row[1], "slug": row[2], "api_key": row[3],
        "allowed_domains": row[4] or "", "active": row[5],
        "flow_config": _parse_flow_config(row[6]),
    }


def get_tenant(tenant_id: int) -> dict | None:
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT id, name, slug, api_key, allowed_domains, active, flow_config FROM tenants WHERE id=?",
        (tenant_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "slug": row[2], "api_key": row[3],
        "allowed_domains": row[4] or "", "active": row[5],
        "flow_config": _parse_flow_config(row[6]),
    }


def save_flow_config(tenant_id: int, flow: dict):
    conn = get_conn(USER_RECORDS_DB)
    conn.execute(
        "UPDATE tenants SET flow_config=? WHERE id=?",
        (json.dumps(flow, ensure_ascii=False), tenant_id),
    )
    conn.commit()
    conn.close()


def seed_initial_data():
    """ساخت tenant نمونه + سوپر ادمین + ادمین نمونه (در صورت نبود)"""
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            api_key TEXT UNIQUE NOT NULL,
            allowed_domains TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            flow_config TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS panel_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            tenant_id INTEGER,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            active INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    # tenant نمونه
    row = cur.execute("SELECT id FROM tenants WHERE slug='demo'").fetchone()
    if not row:
        cur.execute(
            "INSERT INTO tenants (name, slug, api_key, allowed_domains, active, flow_config) VALUES (?,?,?,?,1,?)",
            ("مرکز مشاوره نمونه", "demo", new_api_key(), "", json.dumps(DEFAULT_DEMO_FLOW, ensure_ascii=False)),
        )
        demo_id = cur.lastrowid
    else:
        demo_id = row[0]

    # سوپر ادمین
    if not cur.execute("SELECT 1 FROM panel_users WHERE role='super_admin'").fetchone():
        if SUPER_ADMIN_PASSWORD:
            cur.execute(
                "INSERT INTO panel_users (username, password_hash, role, tenant_id, active) VALUES (?,?,?,NULL,1)",
                (SUPER_ADMIN_USERNAME, hash_password(SUPER_ADMIN_PASSWORD), "super_admin"),
            )
        else:
            import logging
            logging.getLogger(__name__).warning(
                "SUPER_ADMIN_PASSWORD تنظیم نشده — سوپر ادمین ساخته نشد. برای ورود، متغیر را در .env تنظیم کنید."
            )

    # ادمین نمونه
    if not cur.execute("SELECT 1 FROM panel_users WHERE username='demo'").fetchone():
        if ADMIN_PANEL_PASSWORD:
            cur.execute(
                "INSERT INTO panel_users (username, password_hash, role, tenant_id, active) VALUES (?,?,?,?,1)",
                ("demo", hash_password(ADMIN_PANEL_PASSWORD), "admin", demo_id),
            )

    conn.commit()
    conn.close()
    return demo_id
