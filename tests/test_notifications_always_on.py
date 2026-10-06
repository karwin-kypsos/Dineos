"""notifications_enabled stopped being a feature flag on 2026-10-06, per
Karwin: it was on in every plan, so it never told the plans apart - the same
reasoning that removed realtime_enabled on 2026-10-01. Every restaurant
always gets in-app notifications, and the field is gone from the API."""
import pytest
from rest_framework.test import APIClient

from apps.notifications.models import Notification
from apps.notifications.services import notify_role
from apps.platform.models import PlatformAdmin
from apps.restaurant.plans import PLAN_PRESETS

pytestmark = pytest.mark.django_db


def _platform_client():
    PlatformAdmin.objects.create_user(email="super@notifications.test", password="Test@1234")
    client = APIClient()
    login = client.post("/platform/auth/login/", {"email": "super@notifications.test", "password": "Test@1234"}, format="json")
    verify = client.post("/platform/auth/verify-2fa/", {"email": "super@notifications.test", "code": login.data["code"]}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {verify.data['access']}")
    return client


def test_no_plan_lists_notifications_as_a_feature(api_client):
    plans = api_client.get("/v1/auth/plans/").data

    assert all("notifications_enabled" not in preset["flags"] for preset in PLAN_PRESETS.values())
    assert all(f["key"] != "notifications_enabled" for plan in plans for f in plan["features"])


def test_super_admin_has_no_notifications_toggle(restaurant):
    platform = _platform_client()

    flags = platform.get("/platform/feature-flags/").data
    toggle = platform.patch(
        f"/platform/tenants/{restaurant.id}/feature-flags/", {"notifications_enabled": False}, format="json",
    )
    detail = platform.get(f"/platform/tenants/{restaurant.id}/").data

    assert "notifications_enabled" not in str(flags)
    assert toggle.status_code == 400
    assert "notifications_enabled" not in detail


def test_gone_from_me_and_the_qr_landing(admin_client, api_client, restaurant, table):
    _, admin = admin_client

    me = admin.get("/v1/auth/me/").data["restaurant"]
    landing = api_client.get(f"/v1/tables/qr/{restaurant.slug}/{table.table_number}/").data

    assert "notifications_enabled" not in me
    assert "notifications_enabled" not in landing["features"]
    assert me["realtime_enabled"] is True  # untouched


def test_every_restaurant_gets_notifications(admin_client, restaurant):
    admin_user, _ = admin_client

    notify_role(["ADMIN"], tenant=restaurant, type="BILL_REQUESTED", title="Bill requested", body="Table 5")

    assert Notification.objects.filter(recipient=admin_user).exists()
