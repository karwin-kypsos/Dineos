import pytest
from rest_framework.test import APIClient

from apps.authentication.models import User
from apps.platform.models import ImpersonationSession, PlatformActivityLog, PlatformAdmin

pytestmark = pytest.mark.django_db


def _platform_login(client, email, password):
    login = client.post("/platform/auth/login/", {"email": email, "password": password}, format="json")
    verify = client.post("/platform/auth/verify-2fa/", {"email": email, "code": login.data["code"]}, format="json")
    return login, verify


@pytest.fixture
def super_admin():
    return PlatformAdmin.objects.create_user(email="super@2fa.test", password="Test@1234", name="Super")


@pytest.fixture
def platform_client(super_admin):
    client = APIClient()
    _, verify = _platform_login(client, "super@2fa.test", "Test@1234")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {verify.data['access']}")
    return client


def test_login_requires_correct_2fa_code(super_admin):
    client = APIClient()
    login, _ = _platform_login(client, "super@2fa.test", "Test@1234")
    assert login.status_code == 200
    assert login.data["requires_2fa"] is True

    wrong = client.post("/platform/auth/verify-2fa/", {"email": "super@2fa.test", "code": "000000"}, format="json")
    assert wrong.status_code == 400


def test_2fa_code_is_single_use(super_admin):
    client = APIClient()
    login, verify = _platform_login(client, "super@2fa.test", "Test@1234")
    assert verify.status_code == 200

    replay = client.post("/platform/auth/verify-2fa/", {"email": "super@2fa.test", "code": login.data["code"]}, format="json")
    assert replay.status_code == 400


def test_impersonate_mints_working_token_for_target_admin(platform_client, restaurant, admin_client):
    admin_user, _ = admin_client

    response = platform_client.post(f"/platform/tenants/{restaurant.id}/impersonate/")

    assert response.status_code == 201, response.data
    assert response.data["role"] == "ADMIN"
    assert response.data["restaurant_id"] == str(restaurant.id)
    session_id = response.data["impersonation_session_id"]
    session = ImpersonationSession.objects.get(id=session_id)
    assert session.is_active is True
    assert session.target_user_id == admin_user.id

    impersonated_client = APIClient()
    impersonated_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    staff_list = impersonated_client.get("/v1/staff/")
    assert staff_list.status_code == 200

    log = PlatformActivityLog.objects.filter(action="TENANT_IMPERSONATED", restaurant=restaurant).first()
    assert log is not None


def test_impersonate_fails_when_no_admin_exists(platform_client, restaurant):
    User.objects.filter(restaurant=restaurant, role="ADMIN").delete()

    response = platform_client.post(f"/platform/tenants/{restaurant.id}/impersonate/")

    assert response.status_code == 409


def test_ending_impersonation_revokes_token_immediately(platform_client, restaurant, admin_client):
    start = platform_client.post(f"/platform/tenants/{restaurant.id}/impersonate/")
    session_id = start.data["impersonation_session_id"]
    access_token = start.data["access"]

    impersonated_client = APIClient()
    impersonated_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
    before = impersonated_client.get("/v1/staff/")
    assert before.status_code == 200

    end = platform_client.post(f"/platform/impersonation-sessions/{session_id}/end/")
    assert end.status_code == 200
    assert end.data["is_active"] is False

    after = impersonated_client.get("/v1/staff/")
    assert after.status_code == 401

    log = PlatformActivityLog.objects.filter(action="IMPERSONATION_ENDED", restaurant=restaurant).first()
    assert log is not None


def test_regular_staff_token_unaffected_by_impersonation_check(admin_client):
    """A normal (non-impersonation) staff token has no
    impersonation_session_id claim at all — the revocation check must be a
    complete no-op for it."""
    _, client = admin_client

    response = client.get("/v1/auth/me/")

    assert response.status_code == 200
