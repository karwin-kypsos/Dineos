"""Billing visibility for the Super Admin app (2026-10-12, per Karwin):
per-organization invoices, next billing / last payment on organization
responses, and the real subscription_status with a list filter."""
import datetime

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.platform.models import PlatformAdmin
from apps.restaurant.models import Restaurant
from apps.subscriptions.models import Subscription, SubscriptionPayment

pytestmark = pytest.mark.django_db


@pytest.fixture
def platform():
    PlatformAdmin.objects.create_user(email="super@billing.test", password="Test@1234")
    client = APIClient()
    login = client.post("/platform/auth/login/", {"email": "super@billing.test", "password": "Test@1234"}, format="json")
    verify = client.post("/platform/auth/verify-2fa/", {"email": "super@billing.test", "code": login.data["code"]}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {verify.data['access']}")
    return client


_n = iter(range(1, 10_000))


def _sub(restaurant, status, **kw):
    n = next(_n)
    return Subscription.objects.create(restaurant=restaurant, plan_tier=kw.pop("tier", "GROWTH"), amount="2499.00", razorpay_plan_id="plan_b",
                                       razorpay_subscription_id=f"sub_billing_{n}", status=status, **kw)


def _pay(sub, status="PAID", amount="2499.00"):
    n = next(_n)
    return SubscriptionPayment.objects.create(subscription=sub, restaurant=sub.restaurant, razorpay_payment_id=f"pay_billing_{n}",
                                              razorpay_invoice_id=f"inv_billing_{n}", amount=amount, status=status, method="emandate",
                                              plan_tier=sub.plan_tier)


def test_org_invoices_for_the_super_admin(platform, restaurant):
    other = Restaurant.objects.create(name="Other", slug="other-billing")
    sub = _sub(restaurant, "ACTIVE")
    _pay(sub)
    failed = _pay(sub, status="FAILED")
    SubscriptionPayment.objects.filter(id=failed.id).update(created_at=timezone.now() + datetime.timedelta(minutes=1))
    _pay(_sub(other, "ACTIVE"))

    response = platform.get(f"/platform/tenants/{restaurant.id}/invoices/")

    assert response.status_code == 200
    assert response.data["count"] == 2  # only this organization's
    row = response.data["results"][0]
    assert set(row) == {"id", "razorpay_payment_id", "razorpay_invoice_id", "amount", "currency", "status", "method",
                        "plan_tier", "period_start", "period_end", "created_at"}
    assert row["status"] == "FAILED" and row["amount"] == "2499.00"  # newest first


def test_org_invoices_need_a_super_admin(admin_client, restaurant):
    _, client = admin_client

    assert client.get(f"/platform/tenants/{restaurant.id}/invoices/").status_code in (401, 403)


def test_org_detail_shows_next_billing_and_last_payment(platform, restaurant):
    period_end = timezone.now() + datetime.timedelta(days=20)
    sub = _sub(restaurant, "ACTIVE", current_end=period_end, plan_applied_at=timezone.now())
    _pay(sub, amount="2499.00")

    data = platform.get(f"/platform/tenants/{restaurant.id}/").data

    assert data["subscription_status"] == "active"
    assert data["next_billing_date"] is not None and data["next_billing_date"][:10] == period_end.date().isoformat()
    assert data["last_payment_amount"] == "2499.00" and data["last_payment_status"] == "PAID" and data["last_payment_date"]


def test_org_with_no_subscription(platform, restaurant):
    data = platform.get(f"/platform/tenants/{restaurant.id}/").data

    assert data["subscription_status"] == "none"
    assert data["next_billing_date"] is None and data["last_payment_date"] is None
    assert data["last_payment_amount"] is None and data["last_payment_status"] is None


@pytest.mark.parametrize("status, kw, expected", [
    ("AUTHENTICATED", {"start_at": timezone.now() + datetime.timedelta(days=10)}, "trialing"),
    ("AUTHENTICATED", {"start_at": None}, "active"),
    ("ACTIVE", {}, "active"),
    ("PENDING", {}, "past_due"),
    ("HALTED", {}, "past_due"),
    ("CANCELLED", {}, "cancelled"),
])
def test_subscription_status_comes_from_billing_not_the_manual_status(platform, restaurant, status, kw, expected):
    restaurant.status = "PAYMENT_DUE"  # hand-set by a Super Admin
    restaurant.save(update_fields=["status"])
    _sub(restaurant, status, **kw)

    assert platform.get(f"/platform/tenants/{restaurant.id}/").data["subscription_status"] == expected


def test_list_rows_carry_it_and_can_be_filtered(platform, restaurant):
    late = Restaurant.objects.create(name="Late Payer", slug="late-payer")
    _sub(late, "PENDING")
    _sub(restaurant, "ACTIVE")

    rows = platform.get("/platform/tenants/").data["results"]
    at_risk = platform.get("/platform/tenants/?subscription_status=past_due").data
    two = platform.get("/platform/tenants/?subscription_status=past_due,active").data

    assert {r["name"]: r["subscription_status"] for r in rows}["Late Payer"] == "past_due"
    assert [r["name"] for r in at_risk["results"]] == ["Late Payer"] and at_risk["count"] == 1
    assert two["count"] == 2


def test_unknown_subscription_status_filter_is_400(platform):
    response = platform.get("/platform/tenants/?subscription_status=late")

    assert response.status_code == 400 and "subscription_status" in response.data
