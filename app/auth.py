# -*- coding: utf-8 -*-
"""احراز هویت پنل: hash رمز (pbkdf2)، سشن کوکی امضاشده، کنترل نقش"""
import hashlib
import hmac
import secrets

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException

from app.config import SECRET_KEY, ADMIN_SESSION_TTL
from app.db import get_conn
from app.config import USER_RECORDS_DB

if not SECRET_KEY:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "SECRET_KEY تنظیم نشده — کلید موقت برای این اجرا ساخته شد (سشن‌ها با ری‌استارت باطل می‌شوند)."
    )
    SECRET_KEY = secrets.token_hex(32)
_serializer = URLSafeTimedSerializer(SECRET_KEY)
COOKIE_NAME = "panel_session"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 100_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 100_000)
    return hmac.compare_digest(dk.hex(), digest)


def authenticate(username: str, password: str) -> dict | None:
    """خروجی: {id, username, role, tenant_id} یا None"""
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT id, username, password_hash, role, tenant_id, active FROM panel_users WHERE username=?",
        (username,),
    ).fetchone()
    conn.close()
    if not row or not row[5]:
        return None
    if not verify_password(password, row[2]):
        return None
    return {"id": row[0], "username": row[1], "role": row[3], "tenant_id": row[4]}


def make_session_cookie(user: dict) -> str:
    return _serializer.dumps({"uid": user["id"], "role": user["role"], "tid": user["tenant_id"]})


def get_current_user(request: Request) -> dict | None:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    try:
        data = _serializer.loads(cookie, max_age=ADMIN_SESSION_TTL)
    except (BadSignature, SignatureExpired):
        return None
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute(
        "SELECT id, username, role, tenant_id, active FROM panel_users WHERE id=?", (data["uid"],)
    ).fetchone()
    conn.close()
    if not row or not row[4]:
        return None
    return {"id": row[0], "username": row[1], "role": row[2], "tenant_id": row[3]}


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return user


def require_super(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="دسترسی فقط برای سوپر ادمین مجاز است.")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="دسترسی غیرمجاز.")
    return user


def set_session_cookie(resp, user: dict, secure: bool = False):
    resp.set_cookie(
        COOKIE_NAME, make_session_cookie(user), max_age=ADMIN_SESSION_TTL,
        httponly=True, samesite="lax", secure=secure,
    )


def clear_session_cookie(resp):
    resp.delete_cookie(COOKIE_NAME)
