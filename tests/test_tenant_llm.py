# -*- coding: utf-8 -*-
"""پیکربندی LLM اختصاصیِ هر مشتری + فهرست زندهٔ مدل‌ها + تمدید سقف‌ها"""
import json
import re
import uuid

import pytest

from app import llm_flow, llm_service, plan_service, tenants as tenants_mod


# ── fixtureها ──

@pytest.fixture()
def adv_plan(dbs):
    """پلن LLM‌دار که چند مشتری می‌توانند رویش بنشینند"""
    suffix = uuid.uuid4().hex[:8]
    return plan_service.create_plan(
        f"پیشرفته {suffix}", plan_service.MODE_ADVANCED,
        model="plan-model", base_url="https://plan.example/v1",
        api_key="sk-plan-key", message_quota=1000, introduction_quota=50,
    )


@pytest.fixture()
def adv_tenant(dbs, adv_plan):
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(f"مشتری LLM {suffix}", f"llm_{suffix}", "LlmPass123!")
    plan_service.assign_plan(t["tenant_id"], adv_plan, "tester")
    return t["tenant_id"]


@pytest.fixture()
def weak_tenant(dbs):
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(f"مشتری ضعیف {suffix}", f"wllm_{suffix}", "WeakPass12!")
    pid = plan_service.create_plan(f"ضعیف {suffix}", plan_service.MODE_WEAK)
    plan_service.assign_plan(t["tenant_id"], pid, "tester")
    return t["tenant_id"]


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text="", bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class _FakeHttpx:
    """httpx با Client جعلی — تا list_models بدون شبکه تست شود"""

    class TimeoutException(Exception):
        pass

    class HTTPError(Exception):
        pass

    class _Timeout(TimeoutException):
        pass

    class Client:
        resp = None
        exc = None

        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            if _FakeHttpx.Client.exc:
                raise _FakeHttpx.Client.exc()
            return _FakeHttpx.Client.resp

        def post(self, *a, **kw):
            if _FakeHttpx.Client.exc:
                raise _FakeHttpx.Client.exc()
            return _FakeHttpx.Client.resp


@pytest.fixture()
def fake_httpx(monkeypatch):
    def _install(resp=None, exc=None):
        _FakeHttpx.Client.resp = resp
        _FakeHttpx.Client.exc = exc
        monkeypatch.setattr(llm_service, "httpx", _FakeHttpx)
        return _FakeHttpx.Client
    return _install


# ── ۱-۲. فهرست زندهٔ مدل‌ها ──

def test_list_models_parses_openai_shape(fake_httpx):
    fake_httpx(resp=_FakeResp(payload={
        "object": "list",
        "data": [{"id": "m-b"}, {"id": "m-a"}, {"id": "m-a"}, {"id": "m-c"}],
    }))
    models, err = llm_service.list_models("https://api.example/v1", "sk-x")
    assert err == ""
    # مرتب + بدون تکرار — ترتیبِ دریافتیِ درگاه نباید ترتیبِ نمایش را تعیین کند
    assert models == ["m-a", "m-b", "m-c"]


def test_list_models_handles_id_and_name_and_strings(fake_httpx):
    fake_httpx(resp=_FakeResp(payload={"data": [
        {"id": "a"}, {"name": "b"}, "c", {}, None,
    ]}))
    models, err = llm_service.list_models("https://api.example/v1", "sk-x")
    assert err == ""
    assert models == ["a", "b", "c"]


@pytest.mark.parametrize("resp,exc,needle", [
    (_FakeResp(status_code=401, text="nope"), None, "API key"),
    (_FakeResp(status_code=403, text="nope"), None, "API key"),
    (_FakeResp(status_code=404, text="nope"), None, "/models"),
    (_FakeResp(status_code=500, text="boom"), None, "HTTP 500"),
    (_FakeResp(bad_json=True), None, "JSON"),
    (_FakeResp(payload="not-a-list"), None, "ساختار"),
    (None, _FakeHttpx._Timeout, "timeout"),
    (None, _FakeHttpx.HTTPError, "خطای اتصال"),
])
def test_list_models_never_raises(fake_httpx, resp, exc, needle):
    """درگاهِ خراب نباید روت را بترکاند — پیام فارسی کافی است"""
    fake_httpx(resp=resp, exc=exc)
    models, err = llm_service.list_models("https://api.example/v1", "sk-x")
    assert models == []
    assert needle in err


def test_list_models_missing_credentials_is_a_message(fake_httpx):
    fake_httpx(resp=_FakeResp(payload={"data": []}))
    assert llm_service.list_models("", "sk-x")[1]
    assert llm_service.list_models("https://api.example/v1", "")[1]


def test_list_models_empty_list_is_not_an_error(fake_httpx):
    """درگاهی که /models ندارد خطا نیست — مدل را می‌شود دستی نوشت"""
    fake_httpx(resp=_FakeResp(payload={"data": []}))
    models, err = llm_service.list_models("https://api.example/v1", "sk-x")
    assert models == []
    assert err == ""


# ── ۳-۵. خواندن: ارث از پلن در برابر کلیدِ مشتری ──

def test_tenant_without_own_llm_inherits_plan(dbs, adv_tenant):
    """سازگاری: مشتریِ LLM‌دارِ بدون پیکربندیِ اختصاصی مثل قبل از پلن می‌خواند"""
    cfg = llm_flow.get_tenant_llm(adv_tenant)
    assert cfg["base_url"] == "https://plan.example/v1"
    assert cfg["model"] == "plan-model"
    assert cfg["api_key"] == "sk-plan-key"


def test_full_tenant_config_wins_over_plan(dbs, adv_tenant):
    plan_service.set_tenant_llm(
        adv_tenant, "https://mine.example/v1", "my-model", "sk-mine", "tester")
    cfg = llm_flow.get_tenant_llm(adv_tenant)
    assert cfg["base_url"] == "https://mine.example/v1"
    assert cfg["model"] == "my-model"
    assert cfg["api_key"] == "sk-mine"
    # پلن باید دست‌نخورده بماند — این تغییر فقط همین مشتری است
    plan = plan_service.get_plan(plan_service.get_tenant_plan_state(adv_tenant)["plan_id"])
    assert plan["base_url"] == "https://plan.example/v1"
    assert plan_service.get_decrypted_api_key(plan["id"]) == "sk-plan-key"


def test_partial_tenant_config_inherits_plan(dbs, adv_tenant):
    """نیمه‌کاره نباید جایِ ارث را بگیرد — وگرنه مشتریِ فعال بی‌صدا از کار می‌افتد"""
    plan_service.set_tenant_llm(adv_tenant, "", "half-model", "", "tester")
    cfg = llm_flow.get_tenant_llm(adv_tenant)
    assert cfg["model"] == "plan-model"
    assert cfg["base_url"] == "https://plan.example/v1"
    assert cfg["api_key"] == "sk-plan-key"


def test_broken_tenant_key_without_plan_key_is_config_error(dbs, adv_tenant):
    """کلیدِ خراب باید صریح خطا بدهد، نه اینکه بی‌صدا قطع شود"""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    state = plan_service.get_tenant_plan_state(adv_tenant)
    conn.execute("UPDATE plans SET api_key_encrypted='' WHERE id=?", (state["plan_id"],))
    conn.execute("UPDATE tenant_plans SET llm_api_key_encrypted='!!!garbage!!!' "
                 "WHERE tenant_id=?", (adv_tenant,))
    conn.commit()
    conn.close()

    with pytest.raises(llm_flow.LLMConfigError) as e:
        llm_flow.get_tenant_llm(adv_tenant)
    assert "api_key" in str(e.value)


def test_weak_tenant_cannot_be_given_llm(dbs, weak_tenant):
    """پلن ضعیف عمداً بدون LLM است — نباید برایش تنظیمِ درگاه ثبت شود"""
    with pytest.raises(ValueError, match="ضعیف"):
        plan_service.set_tenant_llm(
            weak_tenant, "https://x/v1", "m", "k", "tester")
    assert plan_service.get_tenant_api_key(weak_tenant) == ""


# ── ۶-۸. نوشتن ──

def test_blank_api_key_keeps_stored_key(dbs, adv_tenant):
    plan_service.set_tenant_llm(
        adv_tenant, "https://one.example/v1", "m1", "sk-keep", "tester")
    # تغییرِ درگاه بدون کلید نباید کلیدِ ذخیره‌شده را پاک کند
    plan_service.set_tenant_llm(
        adv_tenant, "https://two.example/v1", "m2", "", "tester")
    assert plan_service.get_tenant_api_key(adv_tenant) == "sk-keep"
    cfg = llm_flow.get_tenant_llm(adv_tenant)
    assert cfg["base_url"] == "https://two.example/v1"
    assert cfg["api_key"] == "sk-keep"


def test_all_blank_returns_to_plan(dbs, adv_tenant):
    plan_service.set_tenant_llm(
        adv_tenant, "https://mine.example/v1", "my-model", "sk-mine", "tester")
    plan_service.set_tenant_llm(adv_tenant, "", "", "", "tester")
    cfg = llm_flow.get_tenant_llm(adv_tenant)
    assert cfg["base_url"] == "https://plan.example/v1"
    assert plan_service.get_tenant_api_key(adv_tenant) == ""
    assert plan_service.get_tenant_llm_config(adv_tenant)["source"] == "plan"


def test_set_llm_rejects_tenant_without_plan(dbs):
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(f"بی‌پلن {suffix}", f"np_{suffix}", "NoPlanPass1!")
    with pytest.raises(ValueError, match="پلن"):
        plan_service.set_tenant_llm(t["tenant_id"], "https://x/v1", "m", "k", "tester")


def test_set_llm_stores_encrypted_not_plain(dbs, adv_tenant):
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    plan_service.set_tenant_llm(
        adv_tenant, "https://x/v1", "m", "sk-plain-text", "tester")
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT llm_api_key_encrypted FROM tenant_plans WHERE tenant_id=?",
                       (adv_tenant,)).fetchone()
    conn.close()
    assert row[0] and "sk-plain-text" not in row[0]


# ── ۹. تمدید سقف‌ها ──

def test_renew_quotas_keeps_custom_ceiling(dbs, adv_tenant):
    """تفاوتِ کلیدی با assign_plan: تمدید نباید سقفِ خریده‌شده را بشکند"""
    plan_service.set_quota_override(adv_tenant, "50", None, "tester")
    assert plan_service.increment_messages(adv_tenant) is True

    plan_service.renew_quotas(adv_tenant, "tester")

    state = plan_service.get_tenant_plan_state(adv_tenant)
    assert state["messages_used"] == 0
    assert state["message_quota_override"] == 50
    assert state["effective_message_quota"] == 50

    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    conn = get_conn(USER_RECORDS_DB)
    row = conn.execute("SELECT last_reset_month FROM tenant_plans WHERE tenant_id=?",
                       (adv_tenant,)).fetchone()
    conn.close()
    # کلید ماه جاری می‌نشیند (خالی = ریست تنبل غیرفعال می‌شد و مشتریِ
    # تمدیدشده تا job ساعتی بعدی قفل می‌ماند)؛ پس باید ماه تهران باشد
    assert row[0] == plan_service._tehran_month()

    # تازه دوباره می‌شود پُر کرد
    assert plan_service.increment_messages(adv_tenant) is True


def test_renew_quotas_rejects_tenant_without_plan(dbs):
    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(f"بی‌پلن تمدید {suffix}", f"np2_{suffix}", "NoPlanPass1!")
    with pytest.raises(ValueError):
        plan_service.renew_quotas(t["tenant_id"], "tester")


# ── ۱۰-۱۱. روت‌های HTTP ──

def _login(client):
    """سقفِ تلاش‌های ورود مشترکِ کل تست‌هاست؛ هر تست پنجرهٔ خودش را می‌گیرد"""
    from app.config import USER_RECORDS_DB
    from app.db import get_conn
    from app import ratelimit
    ratelimit.ensure_table()
    conn = get_conn(USER_RECORDS_DB)
    conn.execute("DELETE FROM rate_limits WHERE key LIKE 'login:%'")
    conn.commit()
    conn.close()

    import os
    r = client.post(
        "/admin/login",
        data={"username": os.environ.get("SUPER_ADMIN_USERNAME", "superadmin"),
              "password": os.environ.get("SUPER_ADMIN_PASSWORD", "test_super_pass")},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def test_llm_route_rejects_weak_and_logs_no_key(dbs, weak_tenant, monkeypatch):
    """پلن ضعیف نباید تنظیم LLM بگیرد، و کلید نباید به لاگ راه پیدا کند"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app import audit_service
    logged = []
    monkeypatch.setattr(
        audit_service, "log", lambda *a, **k: logged.append((a, k)))

    client = TestClient(app, raise_server_exceptions=False)
    _login(client)
    r = client.post(f"/admin/super/tenants/{weak_tenant}/llm",
                    data={"base_url": "https://x/v1", "model": "m",
                          "api_key": "sk-secret-key"},
                    headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "ضعیف" in r.text
    assert plan_service.get_tenant_api_key(weak_tenant) == ""
    # حتی اگر لاگی هم ثبت شده باشد، کلید خام نباید در آن باشد
    for args, kwargs in logged:
        assert "sk-secret-key" not in json.dumps(
            {"a": args, "k": kwargs}, ensure_ascii=False, default=str)


def test_llm_models_route_uses_stored_key(dbs, adv_tenant, monkeypatch):
    """کلید خالی در فرم = کلیدِ ذخیره‌شدهٔ مشتری (مثل plan_test_connection)"""
    from fastapi.testclient import TestClient
    from app.main import app
    plan_service.set_tenant_llm(
        adv_tenant, "https://mine.example/v1", "my-model", "sk-stored-tenant", "tester")

    seen = {}

    def _fake_list_models(base_url, api_key, timeout=None):
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return ["m1", "m2"], ""

    monkeypatch.setattr(llm_service, "list_models", _fake_list_models)
    client = TestClient(app, raise_server_exceptions=False)
    _login(client)
    r = client.post(f"/admin/super/tenants/{adv_tenant}/llm/models",
                    data={"base_url": "", "api_key": ""},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["models"] == ["m1", "m2"]
    # base_url خالی = از تنظیمات ذخیره‌شدهٔ خودِ مشتری
    assert seen["base_url"] == "https://mine.example/v1"
    assert seen["api_key"] == "sk-stored-tenant"


def test_llm_models_route_reports_error_without_raising(dbs, adv_tenant, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(llm_service, "list_models",
                        lambda *a, **k: ([], "API key نامعتبر یا دسترسی غیرمجاز (401/403)."))
    client = TestClient(app, raise_server_exceptions=False)
    _login(client)
    r = client.post(f"/admin/super/tenants/{adv_tenant}/llm/models",
                    data={"base_url": "https://bad.example/v1", "api_key": "sk-bad"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "401" in body["error"]
    assert body["models"] == []


def test_llm_routes_require_super(dbs, adv_tenant):
    """بدون ورود: ریدایرکت به لاگین. با ورودِ غیرسوپر: ۴۰۳. هر دو باید بسته باشند."""
    from fastapi.testclient import TestClient
    from app.main import app
    _login(TestClient(app, raise_server_exceptions=False))  # پنجره را ریست می‌کند
    client = TestClient(app, raise_server_exceptions=False)
    paths = ("/llm", "/llm/models", "/llm/test", "/renew-quotas")
    for path in paths:
        r = client.post(f"/admin/super/tenants/{adv_tenant}{path}",
                        data={}, headers={"Origin": "http://testserver"},
                        follow_redirects=False)
        assert r.status_code in (303, 401, 403), f"{path} → {r.status_code}"
        if r.status_code == 303:
            assert "/admin/login" in r.headers.get("location", "")

    # ادمینِ معمولیِ tenant (نه سوپر) نباید به هیچ‌کدام برسد
    suffix = uuid.uuid4().hex[:8]
    username, password = f"adm_{suffix}", "AdminPass12!"
    tenants_mod.create_tenant(f"ادمین معمولی {suffix}", username, password)
    lr = client.post("/admin/login",
                     data={"username": username, "password": password},
                     headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert lr.status_code == 303, lr.text
    for path in paths:
        r = client.post(f"/admin/super/tenants/{adv_tenant}{path}",
                        data={}, headers={"Origin": "http://testserver"},
                        follow_redirects=False)
        assert r.status_code in (401, 403), f"{path} → {r.status_code}"


def test_super_dashboard_shows_llm_row(dbs, adv_tenant):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    _login(client)
    r = client.get("/admin")
    assert r.status_code == 200
    assert "دریافت مدل‌ها" in r.text
    assert "تمدید سقف‌ها" in r.text
    assert "https://plan.example/v1" in r.text  # نمایشِ ارث‌از-پلن


def test_test_button_sends_the_model_the_admin_typed(dbs, adv_tenant):
    """«تست اتصال» باید مدلِ داخل فرم را بفرستد، نه مدلِ ارث‌از-پلن.

    رگرسیون: مدل در فرمِ «ذخیره» بود و تست آن را نمی‌فرستاد، پس درخواست
    با مدلِ اشتباهِ پلن می‌رفت و کاربر 404 می‌دید در حالی که درگاه سالم بود.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    _login(client)
    r = client.get("/admin")
    form = re.search(
        r'<form[^>]*llm-test-form.*?</form>', r.text, re.S).group(0)
    assert 'name="model"' in form, "فرمِ تست باید خودش فیلدِ مدل داشته باشد"
    # رگرسیون: حذفِ دیتالیست باعث می‌شد fetchModels استثنا بدهد و پیامِ
    # دروغینِ «خطای شبکه» نشان دهد، بی‌آنکه سرور اصلاً خطا داده باشد.
    dl = re.search(r'list="(models-\d+)"', form)
    assert dl, "فیلدِ مدل باید به دیتالیست اشاره کند"
    assert f'<datalist id="{dl.group(1)}">' in r.text, \
        "خودِ <datalist> باید در صفحه باشد تا fetchModels بتواند پرش کند"


def test_test_route_prefers_typed_model_over_plan_model(
        dbs, adv_tenant, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    seen = {}
    monkeypatch.setattr(
        llm_service, "test_connection",
        lambda url, key, model: (seen.update(
            {"url": url, "key": key, "model": model}), (True, "ok"))[1])

    client = TestClient(app, raise_server_exceptions=False)
    _login(client)
    r = client.post(f"/admin/super/tenants/{adv_tenant}/llm/test",
                    data={"base_url": "https://gw.example/v1",
                          "model": "typed-by-admin",
                          "api_key": "sk-typed"},
                    headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 200, r.text
    assert seen == {"url": "https://gw.example/v1",
                    "key": "sk-typed", "model": "typed-by-admin"}


def _select_block(html, tenant_id):
    m = re.search(
        r'<form[^>]*tenants/%d/assign-plan.*?</form>' % tenant_id,
        html, re.S)
    assert m, f"فرمِ تخصیص پلنِ مشتری {tenant_id} یافت نشد"
    return re.search(r'<select name="plan_id".*?</select>', m.group(0),
                     re.S).group(0)


def test_assign_select_marks_current_plan(dbs, adv_tenant, adv_plan):
    """رگرسیون: dropdown باید پلنِ فعلی را selected نشان دهد.

    بدون این، گزینهٔ اولِ فهرست (بزرگ‌ترین id) به‌جای پلنِ واقعی انتخاب
    بود؛ کاربر فکر می‌کرد پلنِ دیگری را می‌فرستد ولی فرم همان فعلی را
    می‌فرستاد و «تغییر پلن» بی‌اثر به نظر می‌رسید.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    _login(client)
    r = client.get("/admin")
    block = _select_block(r.text, adv_tenant)
    sel = re.search(r'<option value="(\d+)" selected[^>]*>([^<]*)</option>',
                    block)
    assert sel, "هیچ گزینه‌ای selected نیست"
    assert int(sel.group(1)) == adv_plan
    assert "(فعلی)" in sel.group(2)

    # تغییر پلن → همان پاسخ باید پلنِ تازه را selected نشان دهد
    other = plan_service.create_plan(
        f"دومین پلن {uuid.uuid4().hex[:8]}", plan_service.MODE_ADVANCED,
        model="m2", base_url="https://b.example/v1", api_key="sk-2",
        message_quota=10, introduction_quota=1)
    r = client.post(f"/admin/super/tenants/{adv_tenant}/assign-plan",
                    data={"plan_id": str(other)},
                    headers={"Origin": "http://testserver"},
                    follow_redirects=True)
    assert r.status_code == 200, r.text
    sel = re.search(r'<option value="(\d+)" selected[^>]*>([^<]*)</option>',
                    _select_block(r.text, adv_tenant))
    assert sel and int(sel.group(1)) == other, \
        "پاسخِ بعد از تخصیص باید پلنِ تازه را selected نشان دهد"


def test_assign_select_without_plan_has_no_selected(dbs):
    """مشتریِ بدون پلن نباید selected داشته باشد — چیزی برای نشان دادن نیست"""
    from fastapi.testclient import TestClient
    from app.main import app

    suffix = uuid.uuid4().hex[:8]
    t = tenants_mod.create_tenant(f"بی‌پلن {suffix}", f"np_{suffix}",
                                  "NoPlanPass1!")
    client = TestClient(app, raise_server_exceptions=False)
    _login(client)
    r = client.get("/admin")
    block = _select_block(r.text, t["tenant_id"])
    assert ' selected' not in block
