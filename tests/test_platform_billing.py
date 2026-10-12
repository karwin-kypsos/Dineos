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



# ---- 2026-10-12: revenue_by_plan on the dashboard (MRR-by-plan chart) -------------

def test_dashboard_revenue_by_plan_month_and_year(platform, restaurant):
    """Per Karwin: monthly = this month's paid subscription charges per plan,
    yearly = this calendar year so far; org_count = organizations on each
    tier now. Failed charges and earlier years don't count."""
    now = timezone.localtime()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    restaurant.plan_tier = "GROWTH"
    restaurant.save(update_fields=["plan_tier"])
    starter_org = Restaurant.objects.create(name="Starter Org", slug="starter-rev", plan_tier="STARTER")
    Restaurant.objects.create(name="Starter Org 2", slug="starter-rev-2", plan_tier="STARTER", status="SUSPENDED")

    growth = _sub(restaurant, "ACTIVE", tier="GROWTH")
    starter = _sub(starter_org, "ACTIVE", tier="STARTER")
    _pay(growth, amount="2499.00")
    _pay(growth, amount="2499.00", status="FAILED")
    _pay(starter, amount="999.00")
    if month_start.month > 1:  # earlier this year: yearly only
        earlier = _pay(growth, amount="2499.00")
        SubscriptionPayment.objects.filter(id=earlier.id).update(created_at=month_start - datetime.timedelta(days=1))
    last_year = _pay(starter, amount="999.00")
    SubscriptionPayment.objects.filter(id=last_year.id).update(created_at=month_start.replace(month=1) - datetime.timedelta(days=1))

    data = platform.get("/platform/dashboard/").data["revenue_by_plan"]

    assert data["month"] == month_start.strftime("%Y-%m") and data["year"] == month_start.year and data["currency"] == "INR"
    monthly = {row["plan_tier"]: row for row in data["monthly"]}
    yearly = {row["plan_tier"]: row for row in data["yearly"]}
    assert [row["plan_tier"] for row in data["monthly"]] == ["STARTER", "GROWTH", "ENTERPRISE"]
    assert monthly["GROWTH"]["revenue"] == "2499.00" and monthly["STARTER"]["revenue"] == "999.00"
    assert monthly["ENTERPRISE"] == {"plan_tier": "ENTERPRISE", "revenue": "0.00", "org_count": 0}
    assert yearly["GROWTH"]["revenue"] == ("4998.00" if month_start.month > 1 else "2499.00")
    assert yearly["STARTER"]["revenue"] == "999.00"  # last year's charge left out
    assert monthly["STARTER"]["org_count"] == yearly["STARTER"]["org_count"] == 2
    assert monthly["GROWTH"]["org_count"] == 1


def test_dashboard_revenue_by_plan_with_no_charges(platform):
    data = platform.get("/platform/dashboard/").data["revenue_by_plan"]

    assert all(row["revenue"] == "0.00" for row in data["monthly"] + data["yearly"])
    assert len(data["monthly"]) == len(data["yearly"]) == 3
