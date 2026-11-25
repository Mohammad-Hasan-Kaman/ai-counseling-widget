# -*- coding: utf-8 -*-
"""ردهٔ فروش و حالتِ پلن باید با هم بخوانند.

سه ردهٔ تجاری روی سه mode می‌نشینند، و گیت‌های قابلیت‌ها مخلوط‌اند:
بعضی `tier` می‌خوانند (گزارش) و بعضی `mode` (یادگیری، پنل چندنفره). اگر
سوپرادمین در فرم — که دو select مستقل دارد — ناسازگاری بسازد، مشتری قابلیت‌هایی را
می‌گیرد که نخریده یا قابلیتی را که خریده از دست می‌دهد.
"""
import uuid

import pytest

from app import plan_service


@pytest.fixture()
def suffix(dbs):
    return uuid.uuid4().hex[:6]


def _mk(suffix, mode, quota=-1, intro=50):
    return plan_service.create_plan(
        f"پلن {suffix}", mode, model="m", base_url="https://api.example.com/v1",
        api_key="sk-x", message_quota=quota, introduction_quota=intro,
        created_by="tester",
    )


def test_mismatched_tier_rejected_on_create(suffix):
    with pytest.raises(ValueError) as e:
        plan_service.create_plan(
            "تناقضی", plan_service.MODE_WEAK,
            message_quota=500, introduction_quota=50, created_by="t",
            tier=plan_service.TIER_PRO,
        )
    assert "سازگار" in str(e.value)


def test_mismatched_tier_rejected_on_update(suffix):
    pid = _mk(suffix, plan_service.MODE_ADVANCED, 500, 50)
    with pytest.raises(ValueError):
        plan_service.update_plan(
            pid, "تناقضی", plan_service.MODE_WEAK, message_quota=500,
            introduction_quota=50, updated_by="t", tier=plan_service.TIER_PRO,
        )
    # پلن نباید تغییری کرده باشد
    assert plan_service.get_plan(pid)["mode"] == plan_service.MODE_ADVANCED
    assert plan_service.get_plan(pid)["tier"] == plan_service.TIER_STANDARD


def test_matching_tiers_accepted(suffix):
    """چهار ترکیبِ معتبر باید بگذرند"""
    for mode, tier in ((plan_service.MODE_WEAK, plan_service.TIER_BASE),
                       (plan_service.MODE_MEDIUM, plan_service.TIER_STANDARD),
                       (plan_service.MODE_ADVANCED, plan_service.TIER_STANDARD),
                       (plan_service.MODE_ADVANCED, plan_service.TIER_PRO)):
        pid = plan_service.create_plan(
            f"{tier} {suffix}", mode, model="m",
            base_url="https://api.example.com/v1", api_key="sk-x",
            message_quota=500, introduction_quota=50, created_by="t", tier=tier)
        assert plan_service.get_plan(pid)["tier"] == tier


def test_rejected_plan_is_not_persisted(suffix):
    """رد شدن نباید ردیف نیمه‌ساخته در DB باقی بگذارد"""
    before = len(plan_service.list_plans())
    with pytest.raises(ValueError):
        plan_service.create_plan(
            f"نیمه {suffix}", plan_service.MODE_WEAK,
            message_quota=500, introduction_quota=50, created_by="t",
            tier=plan_service.TIER_PRO,
        )
    assert len(plan_service.list_plans()) == before


def test_tier_inferred_when_blank(suffix):
    """خالی یعنی از mode و سقف استنتاج شود، نه خطا"""
    pid = plan_service.create_plan(
        f"استنتاج {suffix}", plan_service.MODE_ADVANCED,
        model="m", base_url="https://api.example.com/v1", api_key="sk-x",
        message_quota=500, introduction_quota=50, created_by="t", tier="")
    # سقفِ محدود + advanced ⇒ استاندارد (نه حرفه‌ای)
    assert plan_service.get_plan(pid)["tier"] == plan_service.TIER_STANDARD
