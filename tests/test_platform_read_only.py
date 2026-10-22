"""A Support (Read-only) Super Admin can look but not change anything
(2026-10-22, per Karwin, Telegram msg 1257): every platform request other than
GET/HEAD/OPTIONS is a 403 "Only a Full Admin can do this." - until then only
team management was refused and the app's hidden buttons were all that
stopped the rest. Their own sign-out, password and support-access ending
still work."""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.platform.models import ImpersonationSession, PlatformActivityLog, PlatformAdmin
from apps.platform.serializers import issue_platform_access_token
from apps.restaurant.models import Restaurant

pytestmark = pytest.mark.django_db

REFUSED = {"detail": "Only a Full Admin can do this."}


def _client(admin):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_platform_access_token(admin)}")
    return client


@pytest.fixture
def read_only():
    admin = PlatformAdmin.objects.create_user(email="viewer@ro.test", password="Test@1234", access_level="SUPPORT_READONLY")
    return admin, _client(admin)


@pytest.mark.parametrize("call", [
    lambda c, r: c.post("/platform/tenants/", {"name": "Sneaky Co", "admin_email": "x@sneaky.test"}, format="json"),
    lambda c, r: c.patch(f"/platform/tenants/{r.id}/", {"name": "Renamed"}, format="json"),
    lambda c, r: c.put(f"/platform/tenants/{r.id}/", {"name": "Renamed", "slug": r.slug}, format="json"),
    lambda c, r: c.delete(f"/platform/tenants/{r.id}/"),
    lambda c, r: c.patch(f"/platform/tenants/{r.id}/status/", {"status": "SUSPENDED"}, format="json"),
    lambda c, r: c.patch(f"/platform/tenants/{r.id}/feature-flags/", {"kitchen_enabled": False}, format="json"),
    lambda c, r: c.patch(f"/platform/tenants/{r.id}/plan/", {"plan_tier": "ENTERPRISE"}, format="json"),
    lambda c, r: c.patch(f"/platform/tenants/{r.id}/branding/", {"primary_color": "#000000"}, format="json"),
    lambda c, r: c.post(f"/platform/tenants/{r.id}/impersonate/"),
], ids=["create", "edit", "replace", "delete", "status", "feature-flags", "plan", "branding", "support-access"])
def test_a_read_only_member_cannot_change_an_organization(read_only, restaurant, admin_client, call):
    admin, client = read_only
    before = {f: getattr(restaurant, f) for f in ("name", "status", "plan_tier", "kitchen_enabled", "primary_color", "is_active")}

    resp = call(client, restaurant)

    assert resp.status_code == 403 and resp.data == REFUSED
    restaurant.refresh_from_db()
    assert {f: getattr(restaurant, f) for f in before} == before
    assert not Restaurant.objects.filter(name="Sneaky Co").exists()
    assert not ImpersonationSession.objects.filter(platform_admin=admin).exists()
    assert not PlatformActivityLog.objects.filter(actor=admin).exists()


@pytest.mark.parametrize("path", [
    "/platform/tenants/", "/platform/tenants/{id}/", "/platform/tenants/{id}/invoices/", "/platform/dashboard/",
    "/platform/activity/", "/platform/team/", "/platform/plans/", "/platform/feature-flags/", "/platform/theme/colors/",
    "/platform/auth/me/",
])
def test_a_read_only_member_can_still_look(read_only, restaurant, path):
    _, client = read_only
    assert client.get(path.format(id=restaurant.id)).status_code == 200


def test_a_read_only_member_can_sign_out(read_only):
    _, client = read_only
    assert client.post("/platform/auth/logout/").status_code == 204


def test_a_read_only_member_can_end_their_own_support_access(read_only, restaurant, admin_client):
    """One started before this rule existed: ending it only takes access away."""
    admin, client = read_only
    target, _ = admin_client
    session = ImpersonationSession.objects.create(
        platform_admin=admin, restaurant=restaurant, target_user=target, expires_at=timezone.now() + timezone.timedelta(minutes=30),
    )

    resp = client.post(f"/platform/impersonation-sessions/{session.id}/end/")

    assert resp.status_code == 200
    session.refresh_from_db()
    assert session.ended_at is not None


def test_a_full_admin_can_still_change_things(restaurant):
    client = _client(PlatformAdmin.objects.create_user(email="full@ro.test", password="Test@1234"))

    assert client.patch(f"/platform/tenants/{restaurant.id}/branding/", {"primary_color": "#123456"}, format="json").status_code == 200
    assert client.patch(f"/platform/tenants/{restaurant.id}/feature-flags/", {"kitchen_enabled": False}, format="json").status_code == 200
    restaurant.refresh_from_db()
    assert restaurant.primary_color == "#123456" and restaurant.kitchen_enabled is False


def test_a_member_demoted_mid_session_is_refused_on_the_same_token(restaurant):
    admin = PlatformAdmin.objects.create_user(email="demoted@ro.test", password="Test@1234")
    client = _client(admin)
    PlatformAdmin.objects.filter(id=admin.id).update(access_level="SUPPORT_READONLY")

    resp = client.patch(f"/platform/tenants/{restaurant.id}/branding/", {"primary_color": "#000000"}, format="json")

    assert resp.status_code == 403 and resp.data == REFUSED


def test_a_change_without_a_token_is_still_a_401(api_client, restaurant):
    assert api_client.patch(f"/platform/tenants/{restaurant.id}/branding/", {"primary_color": "#000000"}, format="json").status_code == 401
