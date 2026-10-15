"""Trials really end (2026-10-15, per Karwin, Telegram msg 1224): once
trial_ends_at passes, the first request (staff, kitchen, login, customer QR,
Super Admin list) moves the organization on - ACTIVE when a subscription is
taking over, PAYMENT_DUE when nothing is - instead of "trialing" forever.
Plus: a checkout whose verify call was lost is healed from Razorpay."""
import datetime
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.platform.models import PlatformAdmin
from apps.platform.serializers import issue_platform_access_token
from apps.restaurant.models import Restaurant
from apps.subscriptions.models import Subscription, SubscriptionPayment

pytestmark = pytest.mark.django_db
_n = iter(range(1, 10_000))


def _trial(restaurant, ended=True, plan="GROWTH"):
    restaurant.status = Restaurant.Status.TRIAL
    restaurant.plan_tier = plan
    restaurant.trial_ends_at = timezone.now() + datetime.timedelta(hours=-2 if ended else 48)
    restaurant.save(update_fields=["status", "plan_tier", "trial_ends_at"])
    return restaurant


def _sub(restaurant, status, **kw):
    return Subscription.objects.create(
        restaurant=restaurant, plan_tier=kw.pop("tier", "GROWTH"), amount="2499.00", razorpay_plan_id="plan_t",
        razorpay_subscription_id=f"sub_trial_{next(_n)}", status=status, start_at=kw.pop("start_at", restaurant.trial_ends_at), **kw)


def _super_admin():
    admin = PlatformAdmin.objects.create_user(email="super@trial.test", password="Test@1234")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_platform_access_token(admin)}")
    return client


def test_trial_over_without_a_subscription_needs_a_plan(admin_client, cashier_client, restaurant):
    """Karwin's Starter case: no payment added - stuck on 'trialing' forever."""
    _, admin = admin_client
    _, cashier = cashier_client
    _trial(restaurant, plan="STARTER")

    current = admin.get("/v1/subscriptions/current/")

    assert current.status_code == 200
    assert current.data["status"] == "none" and current.data["organization_status"] == "PAYMENT_DUE"
    restaurant.refresh_from_db()
    assert restaurant.status == "PAYMENT_DUE" and restaurant.is_active is False
    blocked = cashier.get("/v1/orders/")
    assert blocked.status_code == 403 and blocked.json()["code"] == "subscription_required"


def test_trial_over_with_a_subscription_moves_to_active_with_the_first_charge_pending(admin_client, restaurant):
    """Karwin's Growth case: subscribed during the trial, Razorpay's first
    charge due at trial end - no longer 'trialing'."""
    _, admin = admin_client
    _trial(restaurant)
    sub = _sub(restaurant, "AUTHENTICATED", plan_applied_at=timezone.now())

    with patch("core.razorpay_client.fetch_subscription", return_value={"id": sub.razorpay_subscription_id, "status": "authenticated"}), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": []}):
        current = admin.get("/v1/subscriptions/current/").data

    assert current["status"] == "active" and current["organization_status"] == "ACTIVE"
    assert current["first_charge_pending"] is True and current["subscription_id"] == str(sub.id)
    restaurant.refresh_from_db()
    assert restaurant.status == "ACTIVE" and restaurant.is_active is True


def test_first_charge_pending_clears_once_a_charge_is_recorded(admin_client, restaurant):
    _, admin = admin_client
    _trial(restaurant)
    sub = _sub(restaurant, "AUTHENTICATED", plan_applied_at=timezone.now())
    SubscriptionPayment.objects.create(subscription=sub, restaurant=restaurant, razorpay_payment_id="pay_first_t", amount="2499.00",
                                       status="PAID", plan_tier="GROWTH")

    with patch("core.razorpay_client.fetch_subscription", return_value={"id": sub.razorpay_subscription_id, "status": "authenticated"}), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": []}):
        assert admin.get("/v1/subscriptions/current/").data["first_charge_pending"] is False


def test_a_trial_still_running_is_left_alone(admin_client, restaurant):
    _, admin = admin_client
    _trial(restaurant, ended=False)

    current = admin.get("/v1/subscriptions/current/").data

    assert current["status"] == "trialing" and current["organization_status"] == "TRIAL"
    assert current["first_charge_pending"] is False


def test_a_subscription_cancelled_during_the_trial_does_not_carry_on(admin_client, restaurant):
    _, admin = admin_client
    _trial(restaurant)
    _sub(restaurant, "AUTHENTICATED", plan_applied_at=timezone.now(), cancel_at_period_end=True)

    with patch("core.razorpay_client.fetch_subscription", return_value={"status": "authenticated"}), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": []}):
        admin.get("/v1/auth/me/")

    restaurant.refresh_from_db()
    assert restaurant.status == "PAYMENT_DUE"


def test_a_checkout_whose_verify_was_lost_is_healed_at_trial_end(admin_client, restaurant):
    """Org 'b': Razorpay authorised the mandate but our copy stayed CREATED."""
    _, admin = admin_client
    _trial(restaurant, plan="STARTER")
    sub = _sub(restaurant, "CREATED")

    with patch("core.razorpay_client.fetch_subscription", return_value={"id": sub.razorpay_subscription_id, "status": "authenticated"}), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": []}), \
         patch("core.razorpay_client.cancel_subscription", return_value={}):
        current = admin.get("/v1/subscriptions/current/").data

    sub.refresh_from_db()
    restaurant.refresh_from_db()
    assert sub.status == "AUTHENTICATED"
    assert restaurant.status == "ACTIVE" and restaurant.plan_tier == "GROWTH"  # the plan they paid for
    assert current["status"] == "active" and current["plan_id"] == "GROWTH"


def test_a_checkout_whose_verify_was_lost_is_healed_on_the_screen_during_the_trial(admin_client, restaurant):
    from django.core.cache import cache

    _, admin = admin_client
    _trial(restaurant, ended=False, plan="STARTER")
    sub = _sub(restaurant, "CREATED")
    cache.clear()

    with patch("core.razorpay_client.fetch_subscription", return_value={"id": sub.razorpay_subscription_id, "status": "authenticated"}), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": []}), \
         patch("core.razorpay_client.cancel_subscription", return_value={}):
        current = admin.get("/v1/subscriptions/current/").data
    cache.clear()

    sub.refresh_from_db()
    assert sub.status == "AUTHENTICATED" and current["plan_id"] == "GROWTH" and current["status"] == "trialing"


def test_an_abandoned_checkout_never_ends_anything(admin_client, restaurant):
    """A CREATED checkout Razorpay never authorised (or expired/cancelled)
    changes nothing - above all it must not lock the organization out."""
    from django.core.cache import cache

    _, admin = admin_client
    restaurant.status = "ACTIVE"
    restaurant.save(update_fields=["status"])
    sub = _sub(restaurant, "CREATED", start_at=None)
    cache.clear()

    with patch("core.razorpay_client.fetch_subscription", return_value={"id": sub.razorpay_subscription_id, "status": "cancelled"}), \
         patch("core.razorpay_client.fetch_subscription_invoices", return_value={"items": []}):
        response = admin.get("/v1/subscriptions/current/")
    cache.clear()

    sub.refresh_from_db()
    restaurant.refresh_from_db()
    assert response.status_code == 200 and sub.status == "CREATED" and restaurant.status == "ACTIVE"


def test_staff_login_after_the_trial_is_refused(restaurant):
    from django.contrib.auth import get_user_model

    get_user_model().objects.create_user(email="cash@trial.test", password="Test@1234", role="CASHIER", name="C", restaurant=restaurant)
    _trial(restaurant)

    login = APIClient().post("/v1/auth/login/", {"email": "cash@trial.test", "password": "Test@1234"}, format="json")

    assert login.status_code == 400 and "subscription" in str(login.data).lower()


def test_kitchen_tablet_after_the_trial_is_refused(kds_client, restaurant):
    _, kds = kds_client
    _trial(restaurant)

    response = kds.get("/v1/kitchen/devices/me/")

    assert response.status_code == 403 and response.json()["code"] == "subscription_required"


def test_customer_qr_after_the_trial_is_refused(api_client, table, restaurant):
    _trial(restaurant)

    response = api_client.post(f"/v1/tables/{table.id}/session/")

    assert response.status_code == 403
    restaurant.refresh_from_db()
    assert restaurant.status == "PAYMENT_DUE"


def test_super_admin_list_shows_the_trial_ended(restaurant):
    _trial(restaurant)
    client = _super_admin()

    rows = client.get("/platform/tenants/").data["results"]

    row = next(r for r in rows if r["id"] == restaurant.id)
    assert row["status"] == "PAYMENT_DUE"


def test_super_admin_setting_trial_again_gives_a_fresh_window(restaurant, settings):
    _trial(restaurant)
    client = _super_admin()
    client.get("/platform/tenants/")  # the trial ends
    restaurant.refresh_from_db()
    assert restaurant.status == "PAYMENT_DUE"

    resp = client.patch(f"/platform/tenants/{restaurant.id}/status/", {"status": "TRIAL"}, format="json")

    assert resp.status_code == 200 and resp.data["status"] == "TRIAL"
    restaurant.refresh_from_db()
    assert restaurant.trial_ends_at > timezone.now() + datetime.timedelta(days=settings.TRIAL_DAYS) - datetime.timedelta(minutes=1)
    assert client.get("/platform/tenants/").data["results"][0]["status"] == "TRIAL"  # stays a trial
