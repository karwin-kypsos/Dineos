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


def test_creating_staff_without_password_issues_invite(admin_client):
    _, client = admin_client

    response = client.post(
        "/v1/staff/", {"email": "invited@test.dineos", "role": "SERVER", "name": "Invited Server"}, format="json",
    )

    assert response.status_code == 201, response.data
    assert "invite_token" in response.data
    assert response.data["must_change_password"] is True

    user = User.objects.get(email="invited@test.dineos")
    assert user.has_usable_password() is False
    assert PasswordResetToken.objects.filter(user=user).exists()


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


def test_invited_staff_cannot_login_until_invite_accepted(admin_client):
    _, client = admin_client
    create = client.post(
        "/v1/staff/", {"email": "pending@test.dineos", "role": "SERVER", "name": "Pending"}, format="json",
    )
    token = create.data["invite_token"]

    api_client = APIClient()
    blocked_login = api_client.post(
        "/v1/auth/login/", {"email": "pending@test.dineos", "password": "anything"}, format="json",
    )
    assert blocked_login.status_code == 401

    accept = api_client.post(
        "/v1/auth/reset-password/", {"token": token, "new_password": "MyNewPass1"}, format="json",
    )
    assert accept.status_code == 200, accept.data
    assert accept.data["role"] == "SERVER"
    assert "access" in accept.data and "refresh" in accept.data

    user = User.objects.get(email="pending@test.dineos")
    assert user.must_change_password is False
    assert user.has_usable_password() is True

    now_login = api_client.post(
        "/v1/auth/login/", {"email": "pending@test.dineos", "password": "MyNewPass1"}, format="json",
    )
    assert now_login.status_code == 200


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
