# -*- coding: utf-8 -*-
"""تست‌های گزارش دوره‌ای — گیت «فقط پلن حرفه‌ای» + خروجی اکسل."""
import io
import uuid

import pytest

from app import plan_service
from app import report_service
from app import tenants as tenants_mod


@pytest.fixture()
def tenant(dbs):
    suffix = uuid.uuid4().hex[:8]
    created = tenants_mod.create_tenant(
        f"مرکز گزارش {suffix}", f"rep_admin_{suffix}", "RepPass123!"
    )
    # create_tenant نام کاربری ادمین را برنمی‌گرداند → اینجا نگهش می‌داریم برای لاگین
    created["admin_username"] = f"rep_admin_{suffix}"
    return created


def _advanced(name, msg_quota, tier=""):
    return plan_service.create_plan(
        name, plan_service.MODE_ADVANCED, model="m",
        base_url="https://api.example.com/v1", api_key="sk-test",
        message_quota=msg_quota, introduction_quota=80, created_by="tester",
        tier=tier,
    )


def _login(client, tenant):
    """ورود به‌عنوان ادمین خودِ همان مرکز (سوپر ادمین به صفحات مشتری دسترسی ندارد)"""
    r = client.post(
        "/admin/login",
        data={"username": tenant["admin_username"], "password": "RepPass123!"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


# ── گیت ──

def test_report_gate_only_pro_plan(dbs, tenant):
    """حرفه‌ای = advanced + سقف پیام نامحدود؛ استاندارد (سقف ۱۵۰۰) گزارش ندارد"""
    tid = tenant["tenant_id"]
    assert plan_service.tenant_has_report(tid) is False  # بدون پلن

    weak = plan_service.create_plan("پایه گزارش", plan_service.MODE_WEAK,
                                    introduction_quota=30, created_by="tester")
    plan_service.assign_plan(tid, weak, "tester")
    assert plan_service.tenant_has_report(tid) is False

    med = plan_service.create_plan(
        "استاندارد گزارش", plan_service.MODE_MEDIUM, model="m",
        base_url="https://api.example.com/v1", api_key="sk-m",
        message_quota=500, introduction_quota=50, created_by="tester")
    plan_service.assign_plan(tid, med, "tester")
    assert plan_service.tenant_has_report(tid) is False

    std = _advanced("استاندارد گزارش", 1500)  # سقف پیام استاندارد
    plan_service.assign_plan(tid, std, "tester")
    assert plan_service.tenant_has_report(tid) is False, \
        "استاندارد گزارش دوره‌ای ندارد — فقط حرفه‌ای (طبق جدول قیمت)"

    full = _advanced("حرفه‌ای گزارش", plan_service.UNLIMITED)
    plan_service.assign_plan(tid, full, "tester")
    assert plan_service.tenant_has_report(tid) is True


def test_report_gate_ignores_message_quota(dbs, tenant):
    """«حرفه‌ای» گزارش دارد حتی اگر سوپرادمین سقف پیامِ متناهی گذاشته باشد.

    تا پیش از ستون plans.tier، «حرفه‌ای» از روی «سقف پیام = -1» تشخیص داده
    می‌شد؛ یعنی یک اشتباه تایپی در فرم سقف، گزارشِ خریده‌شدهٔ مشتری را بی‌صدا
    خاموش می‌کرد. ردهٔ تجاری از سقف مستقل است."""
    tid = tenant["tenant_id"]
    plan_service.assign_plan(
        tid, _advanced("حرفه‌ای با سقف متناهی", 5000, tier=plan_service.TIER_PRO), "tester")
    assert plan_service.tenant_has_report(tid) is True

    # برعکس: سقف نامحدود به‌تنهایی «حرفه‌ای» نمی‌سازد
    plan_service.assign_plan(
        tid, _advanced("استاندارد با سقف نامحدود", plan_service.UNLIMITED,
                       tier=plan_service.TIER_STANDARD), "tester")
    assert plan_service.tenant_has_report(tid) is False


def test_plan_tier_defaults_from_mode_and_quota(dbs):
    """پلن تازه بدون انتخاب رده = رده‌ای که mode و سقفش نشان می‌دهند"""
    weak = plan_service.create_plan("پایه رده", plan_service.MODE_WEAK,
                                    introduction_quota=30, created_by="tester")
    assert plan_service.get_plan(weak)["tier"] == plan_service.TIER_BASE

    med = plan_service.create_plan(
        "استاندارد رده", plan_service.MODE_MEDIUM, model="m",
        base_url="https://api.example.com/v1", api_key="sk-m",
        message_quota=500, introduction_quota=50, created_by="tester")
    assert plan_service.get_plan(med)["tier"] == plan_service.TIER_STANDARD

    assert plan_service.get_plan(
        _advanced("استاندارد رده", 1500))["tier"] == plan_service.TIER_STANDARD
    assert plan_service.get_plan(
        _advanced("حرفه‌ای رده", plan_service.UNLIMITED))["tier"] == plan_service.TIER_PRO


def test_invalid_tier_rejected(dbs):
    with pytest.raises(ValueError):
        plan_service.create_plan("پلن بد", plan_service.MODE_WEAK,
                                 introduction_quota=30, created_by="t", tier="gold")


def test_sanitize_days(dbs):
    assert report_service.sanitize_days(7) == 7
    assert report_service.sanitize_days(30) == 30
    assert report_service.sanitize_days(90) == 90
    assert report_service.sanitize_days(45) == report_service.DEFAULT_DAYS
    assert report_service.sanitize_days("abc") == report_service.DEFAULT_DAYS
    assert report_service.sanitize_days(None) == report_service.DEFAULT_DAYS


# ── سرویس ──

def test_build_report_empty_tenant(dbs, tenant):
    """tenant بدون داده: صفرها + GHQ بی‌نمونه → None (رندر «—»)"""
    rep = report_service.build_report(tenant["tenant_id"], 7)
    assert rep["conversations"] == 0
    assert rep["leads"] == 0
    assert rep["ghq_avg"] is None
    assert rep["ghq_n"] == 0
    assert len(rep["axis"]) == 7
    assert rep["partial_conversations"] is False


def test_ninety_day_window_flags_partial_conversations(dbs, tenant):
    """بازهٔ ۹۰ روزه: گفتگوها ناقص‌اند (نگهداری ۳۰ روز) → partial + محور کامل"""
    rep = report_service.build_report(tenant["tenant_id"], 90)
    assert rep["partial_conversations"] is True
    assert rep["retention_days"] == 30
    assert len(rep["axis"]) == 90
    assert rep["axis"][-1] == rep["tehran_today"]


def test_build_report_metrics_from_rows(dbs, tenant):
    """درخواست‌ها، مشاوره‌ها، موضوعات و مشاوران برتر از روی ردیف‌های واقعی"""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    tid = tenant["tenant_id"]
    conn = get_conn(USER_RECORDS_DB)
    conn.executemany(
        "INSERT INTO user_requests (tenant_id, full_name, phone, topic, ghq_total, "
        "recommendations, created_at) VALUES (?,?,?,?,?,?, datetime('now'))",
        [
            (tid, "علی", "09120000001", "اضطراب", 18, '[{"name":"دکتر الف","reason":"نزدیک"}]'),
            (tid, "سارا", "09120000002", "اضطراب", 22, '[{"name":"دکتر الف","reason":"نزدیک"}]'),
            (tid, "رضا", "09120000003", "افسردگی", 30, ""),
        ],
    )
    conn.commit()
    conn.close()

    rep = report_service.build_report(tid, 7)
    assert rep["leads"] == 3
    assert rep["consultations"] == 2   # یکی recommendations خالی دارد
    assert rep["ghq_n"] == 3
    assert rep["ghq_avg"] == pytest.approx((18 + 22 + 30) / 3, abs=0.05)
    assert rep["top_topics"][0] == {"name": "اضطراب", "count": 2}
    assert rep["top_consultants"][0] == {"name": "دکتر الف", "count": 2}
    assert sum(rep["daily"]["leads"].values()) == 3
    assert rep["daily"]["leads"][rep["tehran_today"]] == 3


def test_tenant_isolation(dbs, tenant):
    """گزارش یک مرکز نباید دادهٔ مرکز دیگر را قاطی کند"""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    suffix = uuid.uuid4().hex[:8]
    other = tenants_mod.create_tenant(f"مرکز دیگر {suffix}", f"other_{suffix}", "OtherPass123!")
    conn = get_conn(USER_RECORDS_DB)
    conn.execute(
        "INSERT INTO user_requests (tenant_id, full_name, phone, topic) "
        "VALUES (?,?,?,?)", (other["tenant_id"], "مزاحم", "09990000000", "استرس"))
    conn.commit()
    conn.close()
    assert report_service.build_report(tenant["tenant_id"], 7)["leads"] == 0


# ── مسیرها ──

def test_report_page_blocked_for_standard_plan(dbs, tenant):
    """استاندارد: صفحهٔ گزارش به داشبورد برمی‌گردد با پیام «فقط پلن حرفه‌ای»"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    _login(client, tenant)
    plan_service.assign_plan(tenant["tenant_id"], _advanced("استاندارد صفحه", 1500), "tester")

    r = client.get("/admin/report", follow_redirects=True)
    assert r.status_code == 200
    assert "فقط در پلن «حرفه‌ای»" in r.text
    # لینک گزارش هم نباید در ناوبری باشد
    assert "/admin/report/export" not in r.text


def test_report_page_renders_for_pro_plan(dbs, tenant):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    _login(client, tenant)
    plan_service.assign_plan(
        tenant["tenant_id"], _advanced("حرفه‌ای صفحه", plan_service.UNLIMITED), "tester")

    r = client.get("/admin/report")
    assert r.status_code == 200, r.text
    assert "گزارش دوره‌ای" in r.text
    assert "/admin/report/export" in r.text

    # بازهٔ نامعتبر → پیش‌فرض ۷ روز
    r2 = client.get("/admin/report?days=45")
    assert r2.status_code == 200
    # بازهٔ ۹۰ → footnote نگهداری ۳۰ روزه
    r3 = client.get("/admin/report?days=90")
    assert r3.status_code == 200
    assert "نگهداری می‌شوند" in r3.text
    assert 'value="90" selected' in r3.text  # بازهٔ ۹۰ در انتخابگر فعال است


def test_report_export_excel(dbs, tenant):
    """خروجی اکسل: ۲۰۰ + پیوست معتبر با ۴ شیت"""
    from fastapi.testclient import TestClient
    from openpyxl import load_workbook
    from app.main import app
    client = TestClient(app)
    _login(client, tenant)
    plan_service.assign_plan(
        tenant["tenant_id"], _advanced("حرفه‌ای اکسل", plan_service.UNLIMITED), "tester")

    r = client.get("/admin/report/export?days=7")
    assert r.status_code == 200, r.text
    assert "report" in r.headers.get("content-disposition", "")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["خلاصه", "روزانه", "مشاوران برتر", "موضوعات"]
    assert wb["روزانه"].max_row == 8  # هدر + ۷ روز


def test_report_export_blocked_for_non_pro(dbs, tenant):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    _login(client, tenant)
    plan_service.assign_plan(tenant["tenant_id"], _advanced("استاندارد اکسل", 1500), "tester")
    assert client.get("/admin/report/export").status_code == 403


def test_report_requires_login(dbs):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.get("/admin/report", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


# ── ایمنی اکسل ──

def test_export_neutralizes_formula_injection(dbs, tenant):
    """سلول‌هایی که با = شروع شوند نباید فرمول اکسل شوند (_safe_cell)"""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    from openpyxl import load_workbook
    from app.admin_tools import export_report_excel

    tid = tenant["tenant_id"]
    conn = get_conn(USER_RECORDS_DB)
    conn.execute(
        "INSERT INTO user_requests (tenant_id, full_name, phone, topic) VALUES (?,?,?,?)",
        (tid, "=cmd|'/c calc'!A1", "09120000009", "=1+1"))
    conn.commit()
    conn.close()

    data = export_report_excel(tid, 7)
    wb = load_workbook(io.BytesIO(data))
    topics = [c.value for c in wb["موضوعات"]["A"][1:]]
    assert "=1+1" not in topics
    assert "'=1+1" in topics
