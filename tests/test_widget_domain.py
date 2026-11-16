# -*- coding: utf-8 -*-
"""محدودیت دامنه نباید ویجتِ مشتریِ دامنه‌دار را از کار بیندازد.

ویجت داخلِ iframeای بارگذاری می‌شود که خودش از هاستِ ویجت سرو می‌شود
(`/widget` روی هاست ما) و `fetch("/api/chat/…")` هم نسبی است — پس هدر
`Origin` همیشه هاستِ ویجت است، نه سایتِ مشتری. اگر `_resolve_tenant` Origin
مرورگر را با دامنه‌های مجازِ مشتری مقایسه کند، هر مشتریِ دامنه‌دار همیشه
۴۰۳ می‌خورد و ویجتش مرده است.
"""
import uuid

import pytest

from app import plan_service
from app import tenants as tenants_mod


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _tenant(dbs, domains):
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(
        f"دامنه‌دار {suffix}", f"dom_{suffix}", "DomPass123!", domains)
    tid = t["tenant_id"]
    plan_service.assign_plan(tid, plan_service.create_plan(
        f"استاندارد {suffix}", plan_service.MODE_MEDIUM,
        model="m", base_url="https://api.example.com/v1", api_key="sk-test",
        message_quota=500, introduction_quota=50, created_by="tester"), "tester")
    return t


def test_widget_still_works_for_domain_restricted_tenant(dbs):
    """درخواستِ ویجت از هاستِ خودِ ویجت می‌آید، نه از سایتِ مشتری

    ویجت داخلِ iframeای بارگذاری می‌شود که از هاستِ خودِ ویجت سرو شده، پس
    هم `Host` و هم `Origin` همان هاستِ ویجت است."""
    t = _tenant(dbs, "clinic.example")

    client = _client()
    r = client.post(
        "/api/chat/session", json={"api_key": t["api_key"]},
        headers={"Host": "widget.example", "Origin": "https://widget.example"},
    )
    assert r.status_code == 200, (
        f"ویجتِ مشتریِ دامنه‌دار از کار افتاد: {r.status_code} — {r.text[:200]}")


def test_foreign_origin_still_rejected(dbs):
    """اما دامنه‌ی غریبه باید رد شود — این محافظت نباید کور شود"""
    t = _tenant(dbs, "clinic.example")

    client = _client()
    r = client.post(
        "/api/chat/session", json={"api_key": t["api_key"]},
        headers={"Host": "widget.example", "Origin": "https://evil.example"},
    )
    assert r.status_code == 403, f"دامنه‌ی غریبه رد نشد: {r.status_code}"


def test_no_origin_header_is_rejected(dbs):
    """بدون Origin/Referer همچنان رد می‌شود (fail-closed)"""
    t = _tenant(dbs, "clinic.example")

    client = _client()
    r = client.post(
        "/api/chat/session", json={"api_key": t["api_key"]},
        headers={"Host": "widget.example"},
    )
    assert r.status_code == 403, (
        f"بدون Origin باید رد شود (fail-closed): {r.status_code}")
