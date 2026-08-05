import pytest
from rest_framework.test import APIClient

from apps.platform.models import PlatformActivityLog, PlatformAdmin
from apps.platform.serializers import issue_platform_access_token
from apps.restaurant.models import Restaurant


@pytest.fixture
def platform_admin_client():
    admin = PlatformAdmin.objects.create_user(email="super@test.dineos", password="Test@1234", name="Super")
    client = APIClient()
    token = issue_platform_access_token(admin)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return admin, client


@pytest.mark.django_db
class TestDashboard:
    def test_dashboard_requires_platform_auth(self, api_client):
        resp = api_client.get("/platform/dashboard/")
        assert resp.status_code in (401, 403)

    def test_dashboard_returns_summary_counts(self, platform_admin_client):
        _, client = platform_admin_client
        Restaurant.objects.create(name="A", slug="a")
        Restaurant.objects.create(name="B", slug="b", is_active=False)

        resp = client.get("/platform/dashboard/")

        assert resp.status_code == 200
        assert resp.data["total_tenants"] == 2
        assert resp.data["active_tenants"] == 1
        assert resp.data["inactive_tenants"] == 1
        assert len(resp.data["recent_tenants"]) == 2


@pytest.mark.django_db
class TestActivityLog:
    def test_tenant_creation_is_logged(self, platform_admin_client):
        admin, client = platform_admin_client

        resp = client.post("/platform/tenants/", {"name": "New Spot", "slug": "new-spot"})
        assert resp.status_code == 201

        log_resp = client.get("/platform/activity/")
        assert log_resp.status_code == 200
        assert log_resp.data["results"][0]["action"] == "TENANT_CREATED"
        assert "New Spot" in log_resp.data["results"][0]["description"]
        assert log_resp.data["results"][0]["actor_email"] == admin.email

    def test_activity_log_requires_platform_auth(self, api_client):
        resp = api_client.get("/platform/activity/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestTeam:
    def test_create_team_member(self, platform_admin_client):
        _, client = platform_admin_client

        resp = client.post(
            "/platform/team/", {"email": "new-teammate@krypsos.tech", "name": "Teammate", "password": "Strong@1234"}
        )

        assert resp.status_code == 201
        assert "password" not in resp.data
        assert PlatformAdmin.objects.filter(email="new-teammate@krypsos.tech").exists()

        log = PlatformActivityLog.objects.filter(action="TEAM_MEMBER_ADDED").first()
        assert log is not None

    def test_deactivate_team_member(self, platform_admin_client):
        admin, client = platform_admin_client
        other = PlatformAdmin.objects.create_user(email="leaving@krypsos.tech", password="Test@1234")

        resp = client.patch(f"/platform/team/{other.id}/", {"is_active": False})

        assert resp.status_code == 200
        other.refresh_from_db()
        assert other.is_active is False

    def test_team_requires_platform_auth(self, api_client):
        resp = api_client.get("/platform/team/")
        assert resp.status_code in (401, 403)
