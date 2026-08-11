import pytest
from rest_framework.test import APIClient

from apps.authentication.models import PasswordResetToken, User
from apps.platform.models import PlatformAdmin

pytestmark = pytest.mark.django_db


def _platform_login(client, email, password):
    """2-step Super Admin login (password, then 2FA code) — the code comes
    back directly in the step-1 response body since DEBUG=True in tests."""
    login = client.post("/platform/auth/login/", {"email": email, "password": password}, format="json")
    verify = client.post("/platform/auth/verify-2fa/", {"email": email, "code": login.data["code"]}, format="json")
    return verify.data["access"]


def test_creating_staff_without_password_generates_temp_password(admin_client):
    _, client = admin_client

    response = client.post(
        "/v1/staff/", {"email": "invited@test.dineos", "role": "SERVER", "name": "Invited Server"}, format="json",
    )

    assert response.status_code == 201, response.data
    assert "temp_password" in response.data
    assert "invite_token" not in response.data
    assert response.data["must_change_password"] is True

    user = User.objects.get(email="invited@test.dineos")
    # Immediately usable — unlike the org-admin invite flow (see
    # test_organization_creation_with_contact_email_invites_first_admin),
    # staff creation never leaves the account passwordless: Admin has no
    # password field on the Add Staff form, so the backend generates one
    # the account can log in with right away, forcing a change afterward
    # instead of gating login on an emailed link.
    assert user.has_usable_password() is True
    assert not PasswordResetToken.objects.filter(user=user).exists()


def test_creating_staff_with_password_skips_invite(admin_client):
    _, client = admin_client

    response = client.post(
        "/v1/staff/",
        {"email": "direct@test.dineos", "role": "SERVER", "name": "Direct", "password": "Demo@1234"},
        format="json",
    )

    assert response.status_code == 201, response.data
    assert "invite_token" not in response.data
    user = User.objects.get(email="direct@test.dineos")
    assert user.has_usable_password() is True
    assert user.must_change_password is False


def test_invited_staff_logs_in_with_temp_password_then_must_change_it(admin_client):
    _, client = admin_client
    create = client.post(
        "/v1/staff/", {"email": "pending@test.dineos", "role": "SERVER", "name": "Pending"}, format="json",
    )
    temp_password = create.data["temp_password"]

    api_client = APIClient()
    # Login succeeds immediately with the generated temp password — the
    # frontend is expected to see must_change_password: true here and
    # force a change-password screen before loading anything else.
    login = api_client.post(
        "/v1/auth/login/", {"email": "pending@test.dineos", "password": temp_password}, format="json",
    )
    assert login.status_code == 200, login.data
    assert login.data["must_change_password"] is True

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
    change = api_client.patch(
        "/v1/auth/change-password/",
        {"current_password": temp_password, "new_password": "MyNewPass1"},
        format="json",
    )
    assert change.status_code == 200, change.data

    user = User.objects.get(email="pending@test.dineos")
    assert user.must_change_password is False

    now_login = APIClient().post(
        "/v1/auth/login/", {"email": "pending@test.dineos", "password": "MyNewPass1"}, format="json",
    )
    assert now_login.status_code == 200
    assert now_login.data["must_change_password"] is False


def test_organization_creation_with_contact_email_invites_first_admin():
    PlatformAdmin.objects.create_user(email="super@platform.test", password="Test@1234")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_platform_login(client, 'super@platform.test', 'Test@1234')}")

    response = client.post(
        "/platform/tenants/",
        {"name": "Taco Hub", "slug": "taco-hub", "contact_name": "Jamie Lee", "contact_email": "jamie@tacohub.test"},
        format="json",
    )

    assert response.status_code == 201, response.data
    assert "invite_token" in response.data
    assert response.data["plan_tier"] == "STARTER"
    assert response.data["max_branches"] == 1
    assert response.data["kitchen_enabled"] is False  # Starter preset

    admin = User.objects.get(email="jamie@tacohub.test")
    assert admin.role == "ADMIN"
    assert admin.name == "Jamie Lee"
    assert admin.has_usable_password() is False


def test_organization_creation_without_contact_email_skips_admin_creation():
    PlatformAdmin.objects.create_user(email="super3@platform.test", password="Test@1234")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_platform_login(client, 'super3@platform.test', 'Test@1234')}")

    response = client.post("/platform/tenants/", {"name": "No Contact Co", "slug": "no-contact-co"}, format="json")

    assert response.status_code == 201
    assert "invite_token" not in response.data
    assert not User.objects.filter(restaurant_id=response.data["id"]).exists()


def test_growth_plan_preset_applied():
    PlatformAdmin.objects.create_user(email="super4@platform.test", password="Test@1234")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_platform_login(client, 'super4@platform.test', 'Test@1234')}")

    response = client.post(
        "/platform/tenants/", {"name": "Growth Co", "slug": "growth-co", "plan_tier": "GROWTH"}, format="json",
    )

    assert response.status_code == 201
    assert response.data["max_branches"] == 5
    assert response.data["kitchen_enabled"] is True
    assert response.data["realtime_enabled"] is True


def test_starter_plan_blocks_second_branch(admin_client, restaurant):
    _, client = admin_client
    restaurant.plan_tier = "STARTER"
    restaurant.max_branches = 1
    restaurant.save(update_fields=["plan_tier", "max_branches"])

    first = client.post("/v1/branches/", {"name": "Main"}, format="json")
    assert first.status_code == 201

    second = client.post("/v1/branches/", {"name": "Second"}, format="json")
    assert second.status_code == 400
    assert "Starter" in str(second.data)
