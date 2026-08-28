import pytest
from django.contrib.auth import get_user_model

from apps.authentication.serializers import ROLE_METADATA

User = get_user_model()

pytestmark = pytest.mark.django_db


def test_me_includes_own_tenants_feature_flags(admin_client, restaurant):
    _, client = admin_client

    response = client.get("/v1/auth/me/")

    assert response.status_code == 200
    assert response.data["restaurant"]["id"] == restaurant.id
    assert response.data["restaurant"]["slug"] == restaurant.slug
    assert response.data["restaurant"]["kitchen_enabled"] is True
    assert response.data["restaurant"]["billing_enabled"] is True


def test_me_reflects_flags_toggled_by_super_admin(admin_client, restaurant):
    _, client = admin_client
    restaurant.kitchen_enabled = False
    restaurant.save()

    response = client.get("/v1/auth/me/")

    assert response.data["restaurant"]["kitchen_enabled"] is False


def test_me_does_not_expose_billing_rates(admin_client):
    _, client = admin_client

    response = client.get("/v1/auth/me/")

    assert "gst_percentage" not in response.data["restaurant"]
    assert "service_charge_percentage" not in response.data["restaurant"]


def test_me_includes_plan_tier_for_profile_screen(admin_client, restaurant):
    """Requested by Shereena via Telegram (2026-08-28): a single Profile API
    shared by every role needs the org's plan tier alongside its name and
    branch, which /v1/auth/me/ already provided."""
    _, client = admin_client
    restaurant.plan_tier = "ENTERPRISE"
    restaurant.max_branches = None
    restaurant.save(update_fields=["plan_tier", "max_branches"])

    response = client.get("/v1/auth/me/")

    assert response.data["restaurant"]["plan_tier"] == "ENTERPRISE"
    assert response.data["restaurant"]["plan_tier_display"] == "Enterprise"
    assert response.data["restaurant"]["max_branches"] is None


@pytest.mark.parametrize("role, expected_id, expected_name", [
    ("ADMIN",   1, "org_admin"),
    ("MANAGER", 2, "manager"),
    ("SERVER",  3, "server"),
    ("CASHIER", 4, "cashier"),
])
def test_login_response_includes_role_id_and_role_name(
    api_client, restaurant, role, expected_id, expected_name
):
    """POST /v1/auth/login/ must return role_id and role_name alongside the
    existing role field, using the ROLE_METADATA projection."""
    email = f"{role.lower()}@rolemeta.test"
    User.objects.create_user(
        email=email, password="Demo@1234", role=role, name=role.title(),
        restaurant=restaurant,
    )

    response = api_client.post(
        "/v1/auth/login/",
        {"email": email, "password": "Demo@1234"},
        format="json",
    )

    assert response.status_code == 200, response.data
    assert response.data["role"] == role
    assert response.data["role_id"] == expected_id
    assert response.data["role_name"] == expected_name
    # Existing fields still present — additions, not replacements.
    assert response.data["name"] == role.title()
    assert "restaurant_id" in response.data
    assert "access" in response.data
    assert "refresh" in response.data


def test_me_response_includes_role_id_and_role_name(admin_client):
    """GET /v1/auth/me/ must project the role via ROLE_METADATA too."""
    _, client = admin_client
    response = client.get("/v1/auth/me/")

    assert response.status_code == 200
    assert response.data["role"] == "ADMIN"
    assert response.data["role_id"] == ROLE_METADATA["ADMIN"]["id"]
    assert response.data["role_name"] == ROLE_METADATA["ADMIN"]["name"]


def test_role_metadata_covers_every_role_choice():
    """Guardrail: if a new role is added to User.Role, ROLE_METADATA must
    also grow — otherwise the login/Me serializers will KeyError at runtime."""
    assert set(ROLE_METADATA.keys()) == set(User.Role.values)
