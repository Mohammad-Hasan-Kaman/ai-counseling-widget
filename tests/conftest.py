# -*- coding: utf-8 -*-
"""ایزوله‌سازی دیتابیس‌ها: قبل از import هر ماژول اپ، مسیرها به پوشه موقت می‌روند."""
import os
import tempfile
from pathlib import Path

if "USER_RECORDS_DB" not in os.environ:
    _TMP = Path(tempfile.mkdtemp(prefix="nk_test_"))
    os.environ["SECRET_KEY"] = "t" * 64
    os.environ["USER_RECORDS_DB"] = str(_TMP / "user_records.db")
    os.environ["APPOINTMENTS_DB"] = str(_TMP / "appointments.db")
    os.environ["AI_KNOWLEDGE_DB"] = str(_TMP / "ai_knowledge.db")
    os.environ["PROFILES_JSON"] = str(_TMP / "missing_profiles.json")
    os.environ["MAPPING_JSON"] = str(_TMP / "missing_mapping.json")
    os.environ["CRAWLER_ENABLED"] = "0"
    os.environ["ADMIN_PANEL_PASSWORD"] = "test_admin_pass"
    os.environ["SUPER_ADMIN_PASSWORD"] = "test_super_pass"

import pytest  # noqa: E402
from starlette.requests import Request  # noqa: E402


@pytest.fixture(scope="session")
def dbs():
    from app.db import init_all_dbs
    init_all_dbs()
    return True


def make_request(headers: dict | None = None, host: str = "localhost:8000",
                 client: tuple = ("1.2.3.4", 12345)) -> Request:
    hdrs = {"host": host}
    if headers:
        hdrs.update(headers)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/session",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()],
        "client": client,
    }
    return Request(scope)
