"""GET /platform/auth/me/ (2026-10-21, per Karwin, Telegram msgs 1250/1251): the
Super Admin app needs who is signed in and their access_level."""
import pytest
from rest_framework.test import APIClient

from apps.platform.models import PlatformAdmin
from apps.platform.serializers import issue_platform_access_token

pytestmark = pytest.mark.django_db


def _client(admin):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_platform_access_token(admin)}")
    return client


def test_me_for_a_full_admin():
    admin = PlatformAdmin.objects.create_user(email="full@me.test", password="Test@1234", name="Full Admin")

    resp = _client(admin).get("/platform/auth/me/")

    assert resp.status_code == 200
    assert resp.data["email"] == "full@me.test" and resp.data["name"] == "Full Admin"
    assert resp.data["access_level"] == "FULL_ADMIN" and resp.data["can_manage_team"] is True
    assert resp.data["id"] == str(admin.id) and resp.data["is_active"] is True and resp.data["must_change_password"] is False
    assert "password" not in resp.data and "temp_password" not in resp.data


def test_me_for_a_read_only_member():
    admin = PlatformAdmin.objects.create_user(email="ro@me.test", password="Test@1234", access_level="SUPPORT_READONLY")

    resp = _client(admin).get("/platform/auth/me/")

    assert resp.status_code == 200 and resp.data["access_level"] == "SUPPORT_READONLY" and resp.data["can_manage_team"] is False


def test_me_shows_a_changed_access_level_at_once():
    """Why an endpoint and not token claims: the same token, demoted meanwhile."""
    admin = PlatformAdmin.objects.create_user(email="demoted@me.test", password="Test@1234")
    client = _client(admin)
    PlatformAdmin.objects.filter(id=admin.id).update(access_level="SUPPORT_READONLY")

    assert client.get("/platform/auth/me/").data["access_level"] == "SUPPORT_READONLY"


def test_me_reports_a_pending_temporary_password():
    admin = PlatformAdmin.objects.create_user(email="new@me.test", password="Test@1234")
    PlatformAdmin.objects.filter(id=admin.id).update(must_change_password=True)

    assert _client(admin).get("/platform/auth/me/").data["must_change_password"] is True


def test_me_needs_a_super_admin_token(admin_client, api_client):
    _, staff = admin_client
    assert api_client.get("/platform/auth/me/").status_code in (401, 403)
    assert staff.get("/platform/auth/me/").status_code == 401
