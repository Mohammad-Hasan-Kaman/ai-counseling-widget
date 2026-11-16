# -*- coding: utf-8 -*-
"""fetch_page و کراول باید در برابر صفحهٔ خطا و قطعی شبکه محافظه‌کار باشند

- پاسخ 502/403 نباید به‌عنوان HTML معتبر تفسیر شود (وگرنه ردیف‌های سالم
  با «بدون جدول» جایگزین و نوبت‌های آزاد پاک می‌شدند).
- مشاورانی که از سایت حذف شده‌اند فقط بعد از پایانِ کامل کراول پاک شوند؛
  توقف زودهنگام یعنی قطعی شبکه، نه حذف — ردیف‌ها باید بمانند.
"""
import json
import sqlite3

import pytest
import requests

from app.config import APPOINTMENTS_DB
from app import crawler


def _insert(name, tenant_id):
    conn = sqlite3.connect(str(APPOINTMENTS_DB))
    conn.execute(
        "INSERT INTO appointments (counselor_name, tenant_id) VALUES (?, ?)",
        (name, tenant_id),
    )
    conn.commit()
    conn.close()


def _rows(tenant_id):
    conn = sqlite3.connect(str(APPOINTMENTS_DB))
    out = conn.execute(
        "SELECT counselor_name, status FROM appointments WHERE tenant_id=?",
        (tenant_id,),
    ).fetchall()
    conn.close()
    return out


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


def _mapping_file(tmp_path, names):
    path = tmp_path / "mapping.json"
    path.write_text(
        json.dumps({n: f"https://example.com/team/{i}/" for i, n in enumerate(names)},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return str(path)


def test_fetch_page_http_error_raises(monkeypatch):
    """پاسخ خطای HTTP (مثلاً 502 از پروکسی) نباید متن برگرداند"""

    class FakeResp:
        text = "<html>error page</html>"

        def raise_for_status(self):
            raise requests.HTTPError("502 Server Error")

    monkeypatch.setattr(crawler.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)
    with pytest.raises(requests.HTTPError):
        crawler.fetch_page("https://example.com/x")


def test_fetch_page_success_returns_text(monkeypatch):
    class FakeResp:
        text = "<html>ok</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.requests, "get", lambda *a, **k: FakeResp())
    assert crawler.fetch_page("https://example.com/x") == "<html>ok</html>"


def test_crawl_deletes_counselors_removed_from_site(dbs, tmp_path, monkeypatch):
    """پس از پایان کامل، ردیف مشاورانِ دیگرِ فهرست سایت پاک شود —
    نوبتِ شبحی نباید امتیاز مچینگ را بالا ببرد"""
    monkeypatch.setattr(
        crawler, "MAPPING_JSON",
        _mapping_file(tmp_path, ["مریم احمدی", "رضا محمدی"]))
    monkeypatch.setattr(
        crawler, "fetch_page",
        lambda url: "<html><body>بدون جدول</body></html>")
    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)

    _insert("حذف‌شده از سایت", tenant_id=1)

    crawler.crawl_available_slots(tenant_id=1)

    statuses = dict(_rows(1))
    assert "حذف‌شده از سایت" not in statuses
    assert statuses.get("مریم احمدی") == "no_table"
    assert statuses.get("رضا محمدی") == "no_table"


def test_crawl_keeps_rows_when_stopped_early(dbs, tmp_path, monkeypatch):
    """قطعی شبکه (≥۵ خطای متوالی) ≠ حذف از سایت — ردیف‌های قبلی باید بمانند"""
    monkeypatch.setattr(
        crawler, "MAPPING_JSON",
        _mapping_file(tmp_path, [f"مشاور{i}" for i in range(6)]))

    def boom(url):
        raise ConnectionError("net down")

    monkeypatch.setattr(crawler, "fetch_page", boom)
    monkeypatch.setattr(crawler.time, "sleep", lambda s: None)

    _insert("حذف‌شده از سایت", tenant_id=1)

    crawler.crawl_available_slots(tenant_id=1)

    names = [n for n, _ in _rows(1)]
    assert "حذف‌شده از سایت" in names, (
        "کراولِ ناتمام نباید ردیف‌ها را به‌بهانهٔ حذف از سایت پاک کند")
