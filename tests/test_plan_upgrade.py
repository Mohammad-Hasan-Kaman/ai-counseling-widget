# -*- coding: utf-8 -*-
"""ارتقای پلن نباید سقفِ اختصاصیِ قبلی را نگه دارد.

سناریو: مشتری روی پلنِ کوچک سقفِ اختصاصی ۵۰ گرفته. سوپرادمین برایش پلنِ
۵۰۰۰-پیامی تخصیص می‌دهد و پیامِ موفق «شمارنده‌ها ریست شدند» می‌گیرد — ولی
سقفِ اختصاصیِ ۵۰ سرِ جایش می‌مانَد و مشتری بعد از ۵۰ پیام قفل می‌شود، درست
وقتی که ۵۰۰۰ پیام خریده است.
"""
from app import plan_service


def test_upgrade_does_not_keep_previous_custom_ceiling(dbs):
    from app import tenants as tenants_mod
    tenants_mod.seed_initial_data()

    cheap = plan_service.create_plan(
        "تست ارتقا — کوچک", plan_service.MODE_ADVANCED,
        model="m", base_url="u", api_key="k",
        message_quota=100, introduction_quota=10, tier=plan_service.TIER_PRO,
        created_by="test")
    big = plan_service.create_plan(
        "تست ارتقا — بزرگ", plan_service.MODE_ADVANCED,
        model="m", base_url="u", api_key="k",
        message_quota=5000, introduction_quota=90, tier=plan_service.TIER_PRO,
        created_by="test")

    plan_service.assign_plan(1, cheap, by="test")
    plan_service.set_quota_override(1, message_quota="50",
                                    introduction_quota="5", by="test")
    before = plan_service.get_tenant_plan_state(1)
    assert before["effective_message_quota"] == 50, before["effective_message_quota"]

    plan_service.assign_plan(1, big, by="test")
    after = plan_service.get_tenant_plan_state(1)

    # شمارنده‌ها ریست می‌شوند (رفتارِ موجود و درست) …
    assert after["messages_used"] == 0
    assert after["introductions_used"] == 0
    # … اما سقفِ اختصاصی نباید به پلنِ تازه نشت کند.
    assert after["message_quota_override"] is None, (
        f"سقفِ اختصاصیِ {after['message_quota_override']} از پلنِ قبلی "
        f"به پلنِ ۵۰۰۰-پیامی نشت کرد")
    assert after["introduction_quota_override"] is None, (
        f"سقفِ معرفیِ {after['introduction_quota_override']} از پلنِ قبلی نشت کرد")
    assert after["effective_message_quota"] == 5000, after["effective_message_quota"]
    assert after["effective_introduction_quota"] == 90, after["effective_introduction_quota"]

    locked, reason = plan_service.check_lock(1)
    assert not locked, f"مشتریِ ۵۰۰۰-پیامی بعد از ارتقا قفل شد: {reason}"
