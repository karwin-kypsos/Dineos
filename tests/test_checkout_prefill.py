"""prefill on POST /v1/subscriptions/checkout/ (2026-10-20, per Karwin, msgs
1234-1237): every subscription had landed on one Razorpay customer; the app
now passes the organization's own contact details to Razorpay Checkout."""
import pytest

from apps.subscriptions.views import razorpay_contact

pytestmark = pytest.mark.django_db

pytest_plugins = []  # fixtures: admin_client / restaurant from conftest; prices / razorpay below


@pytest.fixture
def prices(settings):
    settings.PLAN_PRICES = {"STARTER": "999", "GROWTH": "2499", "ENTERPRISE": "4999"}


@pytest.fixture
def razorpay():
    from itertools import count
    from unittest.mock import patch

    ids = count(1)
    with patch("core.razorpay_client.create_plan", side_effect=lambda name, amount, period="monthly": {"id": f"plan_p{next(ids)}"}), \
         patch("core.razorpay_client.create_subscription", side_effect=lambda plan_id, total, start_at=None, notes=None: {"id": f"sub_p{next(ids)}"}):
        yield


def test_checkout_returns_the_organizations_own_contact(admin_client, restaurant, prices, razorpay):
    _, client = admin_client
    restaurant.contact_name, restaurant.contact_email, restaurant.billing_email = "Asha Menon", "asha@spice.test", "accounts@spice.test"
    restaurant.contact_phone = "+91 98470 17171"
    restaurant.save()

    resp = client.post("/v1/subscriptions/checkout/", {"plan_id": "GROWTH"}, format="json")

    assert resp.status_code == 201, resp.data
    assert resp.data["prefill"] == {"name": "Asha Menon", "email": "accounts@spice.test", "contact": "+919847017171"}


def test_prefill_falls_back_and_never_guesses(admin_client, restaurant, prices, razorpay):
    _, client = admin_client
    restaurant.contact_name, restaurant.billing_email, restaurant.contact_email, restaurant.contact_phone = "", "", "owner@spice.test", ""
    restaurant.save()

    prefill = client.post("/v1/subscriptions/checkout/", {"plan_id": "STARTER"}, format="json").data["prefill"]

    assert prefill == {"name": restaurant.name, "email": "owner@spice.test", "contact": None}


def test_prefill_email_is_null_when_the_organization_has_none(admin_client, restaurant, prices, razorpay):
    _, client = admin_client
    restaurant.billing_email = restaurant.contact_email = ""
    restaurant.save()

    assert client.post("/v1/subscriptions/checkout/", {"plan_id": "STARTER"}, format="json").data["prefill"]["email"] is None


@pytest.mark.parametrize("raw, expected", [
    ("9847017171", "+919847017171"),
    ("98470 17171", "+919847017171"),
    ("+91 98470 17171", "+919847017171"),
    ("+91-98470-17171", "+919847017171"),
    ("09847017171", "+919847017171"),
    ("919847017171", "+919847017171"),
    ("(+971) 50 123 4567", "+971501234567"),
    ("", None),
    ("12345", None),
    ("not a phone", None),
])
def test_razorpay_contact_format(raw, expected):
    assert razorpay_contact(raw) == expected
