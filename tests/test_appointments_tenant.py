# -*- coding: utf-8 -*-
"""نوبت‌های آزاد باید مالِ همان مرکز باشند، نه یک جدول سراسری.

جدول appointments ستون tenant_id نداشت و load_free_appointments بدون هیچ
فیلتری برای *همهٔ* مشتریان خوانده می‌شد. اثرش نشتِ نام نبود — کاندیداها از
پروفایل‌های خودِ هر مشتری می‌آیند — ولی «🗓 نوبت آزاد: ۴ نوبت» و امتیازِ
نوبتِ آزادِ یک مرکز به مراجعِ مرکزِ دیگر نشت می‌کرد.
"""
import sqlite3

import pytest

from app.config import ADMIN_PANEL_PASSWORD, APPOINTMENTS_DB
from app.internal_ai_engine import load_free_appointments


def _insert(name, tenant_id, status="free", branch="zafar"):
    conn = sqlite3.connect(str(APPOINTMENTS_DB))
    conn.execute(
        "INSERT INTO appointments (counselor_name, date, time, room, status, branch, tenant_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, "2026-09-27", "10:00", "اتاق", status, branch, tenant_id),
    )
    conn.commit()
    conn.close()


def _load(tenant_id):
    """خواندنِ سازگار با هر دو نسخه — تا تست نشت را نشان دهد، نه TypeError

    نسخهٔ قدیم آرگومان نمی‌گیرد و همهٔ مراکز را برمی‌گرداند؛ اینجاست که
    نشت دیده می‌شود."""
    try:
        return load_free_appointments(tenant_id)
    except TypeError:
        return load_free_appointments()


@pytest.fixture(autouse=True)
def clean(dbs):
    conn = sqlite3.connect(str(APPOINTMENTS_DB))
    conn.execute("DELETE FROM appointments")
    conn.commit()
    conn.close()
    yield
    conn = sqlite3.connect(str(APPOINTMENTS_DB))
    conn.execute("DELETE FROM appointments")
    conn.commit()
    conn.close()


def test_appointments_are_scoped_to_their_own_tenant(dbs):
    _insert("مریم احمدی", tenant_id=1)
    _insert("مریم احمدی", tenant_id=2)

    own = _load(1)
    other = _load(2)
    assert len(own) == 1 and own[0][0] == "مریم احمدی"
    assert len(other) == 1


def test_another_tenant_never_sees_foreign_slots(dbs):
    """مشتریِ ۲ نباید نوبتِ مشتریِ ۱ را ببیند — حتی با نام مشترک"""
    _insert("مریم احمدی", tenant_id=1)
    assert _load(2) == [], (
        "نوبتِ مرکزِ دیگر به این مشتری نشت کرد")

    _insert("مریم احمدی", tenant_id=2)
    assert len(_load(2)) == 1


def test_unscoped_read_is_conservative(dbs):
    """خواندنِ بدون tenant فقط ردیف‌های «نامشخص» را می‌بیند

    پیش از این اصلاح، خواندنِ بدون فیلتر همهٔ مراکز را می‌دید. حالا
    tenant_id=None یعنی «نامشخص» — یعنی محافظه‌کارانه، نه فراگیر."""
    _insert("نامشخص", tenant_id=0)
    _insert("مال مرکز نمونه", tenant_id=1)

    rows = _load(None)
    assert [r[0] for r in rows] == ["نامشخص"], (
        "خواندنِ بدون tenant نباید نوبتِ یک مرکزِ مشخص را برگرداند")


def test_only_free_status_counts(dbs):
    _insert("پر", tenant_id=1, status="waiting")
    _insert("آزاد", tenant_id=1, status="free")
    rows = _load(1)
    assert [r[0] for r in rows] == ["آزاد"]


def test_legacy_rows_are_backfilled_to_demo(dbs):
    """نوبت‌هایِ قبل از ستونِ tenant_id باید به مرکز نمونه برسند

    بدون این بک‌فیل، دیتابیسِ پُرِ موجود با فیلترِ جدید «بی‌نوبت» می‌شد
    و تا دورِ بعدِ کراول، «نوبت آزاد» برای مرکز نمونه صفر نشان داده می‌شد."""
    from app import tenants as tenants_mod, crawler
    from app.db import get_conn
    from app.config import USER_RECORDS_DB

    _insert("قدیمی", tenant_id=0)

    conn = get_conn(USER_RECORDS_DB)
    nik = conn.execute("SELECT id FROM tenants WHERE slug='demo'").fetchone()
    if not nik:
        tenants_mod.seed_initial_data()
        nik = conn.execute("SELECT id FROM tenants WHERE slug='demo'").fetchone()
    conn.close()
    assert nik, "پیش‌نیاز: مرکز نمونه باید وجود داشته باشد"

    crawler.backfill_appointment_tenants()

    assert [r[0] for r in _load(nik[0])] == ["قدیمی"], (
        "نوبت‌های بی‌مالک به مرکز نمونه نسبت داده نشد — تا کراولِ بعدی بی‌نوبت است")
    assert _load(0) == []


def test_dashboard_stats_never_show_another_tenants_counselors(dbs):
    """داشبوردِ آمارِ نوبت هم باید به مرکزِ خودش محدود باشد

    خواندنِ موتور مچ بعد از اصلاح فیلتر گرفت، ولی پنج کوئریِ داشبورد
    (admin.py) هنوز بدون فیلتر بودند — یعنی «مشاوران دارای نوبت آزاد»
    روی صفحهٔ مرکز نمونه نامِ مراکزِ دیگر را فاش می‌کرد."""
    from fastapi.testclient import TestClient
    from app import tenants as tenants_mod
    from app.main import app

    # مرکز نمونه (tenant 1) دو نوبت آزاد دارد، مرکزِ دیگر یکی
    _insert("مریم احمدی", tenant_id=1)
    _insert("مریم احمدی", tenant_id=1)
    _insert("علی مرکزی", tenant_id=2)

    tenants_mod.seed_initial_data()

    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(
        "/admin/login",
        data={"username": "demo", "password": ADMIN_PANEL_PASSWORD},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    page = client.get("/admin", follow_redirects=True).text
    assert "علی مرکزی" not in page, (
        "نامِ مشاورِ مرکزِ دیگر روی داشبوردِ مرکز نمونه نشت کرد")
