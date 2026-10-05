# -*- coding: utf-8 -*-
import sqlite3
from pathlib import Path

from app.config import (
    APPOINTMENTS_DB, AI_KNOWLEDGE_DB, USER_RECORDS_DB,
    WIDGET_SESSION_TTL,
)


def get_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_widget_sessions_db():
    conn = get_conn(USER_RECORDS_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS widget_sessions (
            token TEXT PRIMARY KEY,
            state INTEGER NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            tenant_id INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_widget_sessions_expires ON widget_sessions(expires_at)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS widget_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            tenant_id INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_widget_messages_token ON widget_messages(token)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_widget_messages_tenant ON widget_messages(tenant_id)")
    # Legacy databases: add the tenant_id column
    cols = [c[1] for c in cur.execute("PRAGMA table_info(widget_messages)").fetchall()]
    if "tenant_id" not in cols:
        cur.execute("ALTER TABLE widget_messages ADD COLUMN tenant_id INTEGER DEFAULT 1")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_widget_messages_tenant ON widget_messages(tenant_id)")
    conn.commit()
    conn.close()


_TENANT_TABLES = [
    "widget_sessions", "widget_messages", "users", "user_requests",
    "consultants", "upload_history", "concept_feedback",
]


def _migrate_tenant_columns():
    """Add the tenant_id column to all chatbot tables (default 1 = Nikravan)"""
    conn = get_conn(USER_RECORDS_DB)
    for table in _TENANT_TABLES:
        try:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        except Exception:
            continue
        if not cols:
            continue
        if "tenant_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id INTEGER DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant_phone ON users(tenant_id, phone)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON widget_sessions(tenant_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_tenant ON widget_messages(tenant_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_tenant ON user_requests(tenant_id)")
    conn.commit()
    conn.close()


def init_all_dbs():
    from app import user_db
    from app import internal_ai_engine
    from app import crawler
    from app import tenants as tenants_mod

    user_db.init_user_db()
    user_db.init_consultants_db()
    internal_ai_engine.init_learning_db()
    crawler.init_db()
    tenants_mod.seed_initial_data()
    # Session tables are created first because _migrate_tenant_columns builds indexes on those same tables
    init_widget_sessions_db()
    _migrate_tenant_columns()
    _seed_consultants_from_json(user_db)


def _seed_consultants_from_json(user_db):
    """Fill the consultants table from JSON on first run (only when it is empty)"""
    import json
    from app.config import PROFILES_JSON
    if not PROFILES_JSON.exists():
        return
    conn = get_conn(USER_RECORDS_DB)
    count = conn.execute("SELECT COUNT(*) FROM consultants").fetchone()[0]
    conn.close()
    if count > 0:
        return
    profiles = json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    user_db.replace_consultants(profiles, uploaded_by=0)
    init_widget_sessions_db()


def gc_expired_sessions():
    conn = get_conn(USER_RECORDS_DB)
    conn.execute(
        "DELETE FROM widget_sessions WHERE expires_at < datetime('now')"
    )
    # Purge transcripts of deleted sessions + messages older than 30 days
    conn.execute(
        "DELETE FROM widget_messages WHERE token NOT IN (SELECT token FROM widget_sessions)"
    )
    conn.execute(
        "DELETE FROM widget_messages WHERE created_at < datetime('now', '-30 days')"
    )
    conn.commit()
    conn.close()
