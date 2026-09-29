"""Admin Self-Registration (2026-09-29): Get Plans -> Register Restaurant ->
Signup -> Login, all public, per Shereena's spec relayed by Karwin."""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.models import User
from apps.platform.models import PlatformActivityLog
from apps.restaurant.models import Restaurant, RestaurantRegistration

pytestmark = pytest.mark.django_db

VALID = {
    "restaurant_name": "Spice Route Kochi",
    "contact_name": "Anita Menon",
    "contact_phone": "+91 98470 12345",
    "contact_email": "anita@spiceroute.example",
    "billing_email": "accounts@spiceroute.example",
    "gst_number": "32abcde1234f1z5",
    "service_charge_percentage": "5.00",
    "plan_id": "GROWTH",
}


@pytest.fixture(autouse=True)
def fresh_throttle():
    # The rate limit counts in the cache, which outlives a single test.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client():
    return APIClient()


def _register(client, **overrides):
    return client.post("/v1/auth/register-restaurant/", {**VALID, **overrides}, format="json")


def _signup(client, registration_id, email="anita@spiceroute.example", password="Secret@123", confirm=None):
    return client.post(
        "/v1/auth/signup/",
        {"registration_id": registration_id, "email": email, "password": password,
         "password_confirm": password if confirm is None else confirm},
        format="json",
    )


def _registered_restaurant(client, **overrides):
    registration_id = _register(client, **overrides).data["registration_id"]
    response = _signup(client, registration_id, email=overrides.get("contact_email", VALID["contact_email"]))
    assert response.status_code == 201, response.data
    return Restaurant.objects.get(id=response.data["restaurant_id"]), response


# ---- 1. Get Plans ----------------------------------------------------------------

def test_plans_are_public_and_describe_each_tier(client):
    # A stale token left in the app must not turn the plans list into a 401.
    client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")

    response = client.get("/v1/auth/plans/")

    assert response.status_code == 200
    plans = {p["id"]: p for p in response.data}
    assert list(plans) == ["STARTER", "GROWTH", "ENTERPRISE"]
    assert plans["STARTER"]["name"] == "Starter"
    assert plans["STARTER"]["max_branches"] == 1
    assert plans["ENTERPRISE"]["max_branches"] is None  # unlimited
    starter_features = {f["key"]: f["included"] for f in plans["STARTER"]["features"]}
    # Billing left Starter on 2026-09-29, per Karwin.
    assert starter_features == {
        "kitchen_enabled": False, "billing_enabled": False, "notifications_enabled": True, "realtime_enabled": False,
    }
    assert all(f["label"] and f["description"] for f in plans["GROWTH"]["features"])
    # No real prices yet: null, never a made-up number.
    assert all(p["price"] is None and p["billing_cycle"] is None and p["currency"] == "INR" for p in plans.values())


# ---- 2. Register Restaurant ------------------------------------------------------

def test_register_returns_a_registration_to_sign_up_with(client):
    restaurants_before = Restaurant.objects.count()

    response = _register(client)

    assert response.status_code == 201, response.data
    assert response.data["status"] == "PENDING_SIGNUP"
    assert response.data["next_step"] == "signup"
    assert response.data["plan"]["id"] == "GROWTH"
    registration = RestaurantRegistration.objects.get(id=response.data["registration_id"])
    assert registration.gst_number == "32ABCDE1234F1Z5"
    assert registration.billing_email == "accounts@spiceroute.example"
    # Nothing on the platform until the Admin sets a login.
    assert Restaurant.objects.count() == restaurants_before


def test_register_defaults_billing_email_and_allows_no_gstin(client):
    response = _register(client, billing_email="", gst_number="")

    assert response.status_code == 201, response.data
    registration = RestaurantRegistration.objects.get(id=response.data["registration_id"])
    assert registration.billing_email == VALID["contact_email"]
    assert registration.gst_number == ""


@pytest.mark.parametrize("field, value", [
    ("restaurant_name", ""),
    ("contact_name", ""),
    ("contact_phone", "abcdefghij"),
    ("contact_email", "not-an-email"),
    ("gst_number", "12345"),
    ("service_charge_percentage", "150"),
    ("plan_id", "PLATINUM"),
])
def test_register_rejects_bad_input(client, field, value):
    response = _register(client, **{field: value})

    assert response.status_code == 400
    assert field in response.data
    assert RestaurantRegistration.objects.count() == 0


# ---- 3. Signup, and 4. Login -------------------------------------------------------

def test_signup_creates_the_restaurant_and_its_admin(client):
    registration_id = _register(client).data["registration_id"]

    response = _signup(client, registration_id)

    assert response.status_code == 201, response.data
    assert response.data["role"] == "ADMIN"
    assert response.data["restaurant_status"] == "TRIAL"
    restaurant = Restaurant.objects.get(id=response.data["restaurant_id"])
    assert restaurant.name == "Spice Route Kochi"
    assert restaurant.slug == "spice-route-kochi"
    assert restaurant.status == Restaurant.Status.TRIAL
    assert timedelta(days=13) < restaurant.trial_ends_at - timezone.now() <= timedelta(days=14)
    assert restaurant.plan_tier == "GROWTH"
    assert restaurant.max_branches == 5
    assert restaurant.kitchen_enabled and restaurant.realtime_enabled
    assert restaurant.gst_number == "32ABCDE1234F1Z5"
    assert restaurant.service_charge_percentage == Decimal("5.00")
    assert (restaurant.contact_name, restaurant.contact_phone) == ("Anita Menon", "+91 98470 12345")
    assert restaurant.billing_email == "accounts@spiceroute.example"

    admin = User.objects.get(id=response.data["user_id"])
    assert admin.role == User.Role.ADMIN
    assert admin.restaurant_id == restaurant.id
    assert admin.name == "Anita Menon"
    assert admin.check_password("Secret@123")

    registration = RestaurantRegistration.objects.get(id=registration_id)
    assert registration.status == RestaurantRegistration.Status.COMPLETED
    assert registration.restaurant_id == restaurant.id

    log = PlatformActivityLog.objects.get(restaurant=restaurant)
    assert log.action == "TENANT_CREATED" and log.actor is None


def test_self_registered_admin_logs_in_as_admin(client):
    restaurant, _ = _registered_restaurant(client)

    response = client.post(
        "/v1/auth/login/", {"email": VALID["contact_email"], "password": "Secret@123"}, format="json",
    )

    assert response.status_code == 200, response.data
    assert response.data["role"] == "ADMIN"
    assert response.data["restaurant_id"] == str(restaurant.id)


def test_starter_plan_applies_its_limits(client):
    restaurant, _ = _registered_restaurant(client, plan_id="STARTER")

    assert restaurant.max_branches == 1
    assert restaurant.kitchen_enabled is False
    assert restaurant.realtime_enabled is False
    assert restaurant.billing_enabled is False
    assert restaurant.notifications_enabled is True


def test_signup_refuses_mismatched_or_short_passwords(client):
    registration_id = _register(client).data["registration_id"]

    mismatch = _signup(client, registration_id, password="Secret@123", confirm="Secret@124")
    short = _signup(client, registration_id, password="abc")

    assert mismatch.status_code == 400 and "password_confirm" in mismatch.data
    assert short.status_code == 400 and "password" in short.data
    assert not Restaurant.objects.filter(name=VALID["restaurant_name"]).exists()


def test_signup_refuses_an_email_already_in_use(client, restaurant):
    User.objects.create_user(
        email="Taken@Example.com", password="Test@1234", role="MANAGER", name="Existing", restaurant=restaurant,
    )
    registration_id = _register(client).data["registration_id"]

    response = _signup(client, registration_id, email="taken@example.com")

    assert response.status_code == 400
    assert "email" in response.data
    assert not Restaurant.objects.filter(name=VALID["restaurant_name"]).exists()


def test_signup_only_works_once(client):
    registration_id = _register(client).data["registration_id"]
    assert _signup(client, registration_id).status_code == 201

    again = _signup(client, registration_id, email="someone-else@example.com")

    assert again.status_code == 409
    assert Restaurant.objects.filter(name=VALID["restaurant_name"]).count() == 1


def test_unknown_registration_is_not_found(client):
    assert _signup(client, str(uuid.uuid4())).status_code == 404


def test_expired_registration_is_refused(client):
    registration_id = _register(client).data["registration_id"]
    RestaurantRegistration.objects.filter(id=registration_id).update(expires_at=timezone.now() - timedelta(minutes=1))

    response = _signup(client, registration_id)

    assert response.status_code == 400
    assert "registration_id" in response.data
    assert not Restaurant.objects.filter(name=VALID["restaurant_name"]).exists()


def test_same_restaurant_name_gets_its_own_slug(client):
    first, _ = _registered_restaurant(client)
    second, _ = _registered_restaurant(client, contact_email="second@spiceroute.example")

    assert first.slug == "spice-route-kochi"
    assert second.slug == "spice-route-kochi-2"


def test_registration_is_rate_limited(client):
    for _ in range(20):
        assert _register(client).status_code == 201

    assert _register(client).status_code == 429
