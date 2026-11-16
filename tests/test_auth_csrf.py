# -*- coding: utf-8 -*-
"""تست‌های احراز هویت و CSRF"""
from app.auth import hash_password, verify_password


def test_password_hash_roundtrip():
    stored = hash_password("s3cret-pass")
    assert stored != "s3cret-pass"
    assert "$" in stored
    assert verify_password("s3cret-pass", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret-pass", "corrupted")


def test_csrf_rejects_admin_post_without_origin(dbs):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.post("/admin/login", data={"username": "x", "password": "y"})
    assert resp.status_code == 403


def test_csrf_rejects_cross_origin_admin_post(dbs):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/admin/login",
        data={"username": "x", "password": "y"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_csrf_allows_same_origin_admin_post(dbs):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/admin/login",
        data={"username": "nouser", "password": "badpass"},
        headers={"Origin": "http://testserver"},
    )
    # از CSRF عبور کرد؛ احراز هویت شکست می‌خورد (401) نه 403
    assert resp.status_code == 401
