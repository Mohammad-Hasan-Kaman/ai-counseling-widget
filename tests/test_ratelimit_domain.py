# -*- coding: utf-8 -*-
"""تست‌های rate limit دیتابیس‌محور، client_ip با XFF و domain allowlist"""
import pytest
from fastapi import HTTPException

from app.ratelimit import client_ip, rate_ok
from tests.conftest import make_request


def test_rate_limit_blocks_after_limit(dbs):
    key = "test:unit:rate"
    results = [rate_ok(key, 3, 60) for _ in range(5)]
    assert results == [True, True, True, False, False]


def test_client_ip_trusted_proxy_last_hop():
    """پشت nginx: آخرین هاپ XFF (اضافه‌شده توسط proxy) ملاک است — هاپ اول جعل‌پذیر"""
    req = make_request(
        headers={"x-forwarded-for": "6.6.6.6, 203.0.113.9"},
        client=("127.0.0.1", 12345),
    )
    assert client_ip(req) == "203.0.113.9"


def test_client_ip_ignores_xff_from_untrusted_peer():
    """بدون proxy معتبر: XFF نادیده گرفته می‌شود (جلوی دور زدن rate limit)"""
    req = make_request(headers={"x-forwarded-for": "6.6.6.6"}, client=("8.8.8.8", 12345))
    assert client_ip(req) == "8.8.8.8"


def test_client_ip_falls_back_to_peer():
    req = make_request()
    assert client_ip(req) == "1.2.3.4"


# ── domain allowlist ──

def test_host_allowed_exact_and_subdomain():
    from app.routers.chat import _host_allowed
    assert _host_allowed("example.com", ["example.com"])
    assert _host_allowed("www.example.com", ["example.com"])
    assert _host_allowed("widget.example.com", ["example.com"])
    assert not _host_allowed("example.com.evil.net", ["example.com"])
    assert not _host_allowed("notexample.com", ["example.com"])


def test_resolve_unknown_key_401(dbs):
    from app.routers.chat import _resolve_tenant
    with pytest.raises(HTTPException) as e:
        _resolve_tenant("nk_deadbeef", make_request())
    assert e.value.status_code == 401


def test_domain_fail_closed_without_origin(dbs):
    from app import tenants as tenants_mod
    from app.routers.chat import _resolve_tenant
    created = tenants_mod.create_tenant(
        "شرکت تست دامنه", "domain_admin", "DomainPass1!", allowed_domains="example.com"
    )
    key = created["api_key"]

    # بدون Origin/Referer → رد (fail-closed)
    with pytest.raises(HTTPException) as e:
        _resolve_tenant(key, make_request())
    assert e.value.status_code == 403

    # Origin مجاز → قبول
    tenant = _resolve_tenant(key, make_request(headers={"origin": "https://example.com"}))
    assert tenant["id"] == created["tenant_id"]

    # ساب‌دامین مجاز → قبول
    tenant = _resolve_tenant(key, make_request(headers={"origin": "https://shop.example.com"}))
    assert tenant["id"] == created["tenant_id"]

    # دامنه غیرمجاز → رد
    with pytest.raises(HTTPException) as e:
        _resolve_tenant(key, make_request(headers={"origin": "https://evil.com"}))
    assert e.value.status_code == 403


def test_domain_empty_config_allows_any(dbs):
    # tenant بدون محدودیت دامنه (allowed_domains خالی) بدون Origin هم قبول است
    from app import tenants as tenants_mod
    from app.routers.chat import _resolve_tenant
    created = tenants_mod.create_tenant("شرکت آزاد دامنه", "free_admin", "FreePass1!")
    tenant = _resolve_tenant(created["api_key"], make_request())
    assert tenant["id"] == created["tenant_id"]
