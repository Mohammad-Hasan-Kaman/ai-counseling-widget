# -*- coding: utf-8 -*-
"""رمزنگاری API key های LLM در دیتابیس (Fernet متکی به SECRET_KEY) — تصمیم Q28"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import SECRET_KEY


class EncryptionError(Exception):
    pass


def _fernet() -> Fernet:
    if not SECRET_KEY:
        raise EncryptionError("SECRET_KEY تنظیم نشده — رمزنگاری ممکن نیست.")
    key = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_str(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_str(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise EncryptionError("رمزگشایی API key ناموفق بود (SECRET_KEY عوض شده؟)") from e
