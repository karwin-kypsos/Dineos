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

    def test_dashboard_includes_monthly_revenue_and_branch_count(self, platform_admin_client):
        from apps.billing.models import Bill
        from apps.orders.models import Order
        from apps.restaurant.models import Branch

        _, client = platform_admin_client
        restaurant = Restaurant.objects.create(name="Revenue Co", slug="revenue-co")
        branch = Branch.objects.create(restaurant=restaurant, name="Main")
        order = Order.objects.create(order_type="TAKEAWAY", branch=branch, round_number=1)
        Bill.objects.create(
            order=order, branch=branch, subtotal="100.00", total_amount="100.00", payment_method="CASH",
        )

        resp = client.get("/platform/dashboard/")

        assert resp.status_code == 200
        assert float(resp.data["monthly_revenue"]) >= 100.0
        assert resp.data["total_branches"] >= 1
        assert len(resp.data["new_signups_this_week"]) == 7

    def test_dashboard_lists_suspended_orgs_as_needing_attention(self, platform_admin_client):
        _, client = platform_admin_client
        Restaurant.objects.create(name="Suspended Co", slug="suspended-co", status=Restaurant.Status.SUSPENDED)
        Restaurant.objects.create(name="Healthy Co", slug="healthy-co", status=Restaurant.Status.ACTIVE)

        resp = client.get("/platform/dashboard/")

        assert resp.status_code == 200
        assert resp.data["organizations_needing_attention_count"] == 1
        names = {org["name"] for org in resp.data["organizations_needing_attention"]}
        assert names == {"Suspended Co"}


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
class TestTenantDelete:
    def _seed(self):
        from apps.authentication.models import User
        from apps.menu.models import Category
        from apps.restaurant.models import Branch

        restaurant = Restaurant.objects.create(name="To Delete", slug="to-delete")
        Branch.objects.create(restaurant=restaurant, name="Main")
        User.objects.create_user(email="staff@to-delete.demo", password="Test@1234", role="MANAGER", name="Manager", restaurant=restaurant)
        Category.objects.create(restaurant=restaurant, name="Mains")
        return restaurant

    def test_delete_without_confirm_returns_409_and_does_not_delete(self, platform_admin_client):
        _, client = platform_admin_client
        restaurant = self._seed()

        resp = client.delete(f"/platform/tenants/{restaurant.id}/")

        assert resp.status_code == 409
        assert resp.data["organization"] == "To Delete"
        assert resp.data["will_delete"]["staff_count"] == 1
        assert resp.data["will_delete"]["branches_count"] == 1
        assert resp.data["will_delete"]["menu_categories_count"] == 1
        assert Restaurant.objects.filter(id=restaurant.id).exists()

    def test_delete_with_confirm_deletes_everything_and_logs_it(self, platform_admin_client):
        admin, client = platform_admin_client
        restaurant = self._seed()
        restaurant_id = restaurant.id

        resp = client.delete(f"/platform/tenants/{restaurant_id}/?confirm=true")

        assert resp.status_code == 204
        assert not Restaurant.objects.filter(id=restaurant_id).exists()

        log = PlatformActivityLog.objects.filter(action="TENANT_DELETED").first()
        assert log is not None
        assert "To Delete" in log.description
        assert log.restaurant is None  # SET_NULL after the cascade
        assert log.actor == admin

    def test_delete_requires_platform_auth(self, api_client):
        restaurant = Restaurant.objects.create(name="Protected", slug="protected-delete")
        resp = api_client.delete(f"/platform/tenants/{restaurant.id}/?confirm=true")
        assert resp.status_code in (401, 403)
        assert Restaurant.objects.filter(id=restaurant.id).exists()


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

    def test_remove_team_member_returns_confirmation_body(self, platform_admin_client):
        admin, client = platform_admin_client
        other = PlatformAdmin.objects.create_user(email="removed@krypsos.tech", password="Test@1234")

        resp = client.delete(f"/platform/team/{other.id}/")

        assert resp.status_code == 200
        assert resp.data["detail"] == "Team member 'removed@krypsos.tech' removed successfully."
        assert not PlatformAdmin.objects.filter(id=other.id).exists()

        log = PlatformActivityLog.objects.filter(action="TEAM_MEMBER_REMOVED").first()
        assert log is not None
        assert "removed@krypsos.tech" in log.description
