"""realtime_enabled stopped being a feature flag on 2026-10-01, per Karwin:
every restaurant on every plan gets live updates. It is gone from the plans,
the Super Admin's toggles and the database, but the apps that read
realtime_enabled still get true."""
import pytest
from rest_framework.test import APIClient

from apps.platform.models import PlatformAdmin
from apps.restaurant.plans import PLAN_PRESETS

pytestmark = pytest.mark.django_db


def _platform_client():
    PlatformAdmin.objects.create_user(email="super@realtime.test", password="Test@1234")
    client = APIClient()
    login = client.post("/platform/auth/login/", {"email": "super@realtime.test", "password": "Test@1234"}, format="json")
    verify = client.post("/platform/auth/verify-2fa/", {"email": "super@realtime.test", "code": login.data["code"]}, format="json")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {verify.data['access']}")
    return client


def test_no_plan_lists_realtime_as_a_feature(api_client):
    plans = api_client.get("/v1/auth/plans/").data

    assert all("realtime_enabled" not in preset["flags"] for preset in PLAN_PRESETS.values())
    assert all(f["key"] != "realtime_enabled" for plan in plans for f in plan["features"])


def test_super_admin_has_no_realtime_toggle(restaurant):
    platform = _platform_client()

    flags = platform.get("/platform/feature-flags/").data
    toggle = platform.patch(
        f"/platform/tenants/{restaurant.id}/feature-flags/", {"realtime_enabled": False}, format="json",
    )
    detail = platform.get(f"/platform/tenants/{restaurant.id}/").data

    assert "realtime_enabled" not in str(flags)
    assert toggle.status_code == 400
    assert detail["realtime_enabled"] is True


def test_apps_that_read_the_flag_still_see_it_on(admin_client, api_client, restaurant, table):
    _, admin = admin_client

    me = admin.get("/v1/auth/me/").data["restaurant"]
    landing = api_client.get(f"/v1/tables/qr/{restaurant.slug}/{table.table_number}/").data

    assert me["realtime_enabled"] is True
    assert landing["features"]["realtime_enabled"] is True
