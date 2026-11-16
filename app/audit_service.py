# -*- coding: utf-8 -*-
"""لاگ ممیزی اقدامات حساس (تصمیم Q46) — جدول audit_log، نمایش فقط سوپرادمین"""
import json

from app.config import USER_RECORDS_DB
from app.db import get_conn


def log(username: str, action: str, details: dict | None = None):
    conn = get_conn(USER_RECORDS_DB)
    try:
        conn.execute(
            "INSERT INTO audit_log(username, action, details) VALUES (?,?,?)",
            (username or "", action, json.dumps(details or {}, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def recent(limit: int = 200) -> list[dict]:
    conn = get_conn(USER_RECORDS_DB)
    rows = conn.execute(
        "SELECT username, action, details, created_at FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            details = json.loads(r[2] or "{}")
        except ValueError:
            details = {}
        out.append({"username": r[0], "action": r[1], "details": details, "created_at": r[3]})
    return out
