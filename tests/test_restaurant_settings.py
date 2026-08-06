from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


def test_admin_can_view_gst_settings(admin_client, restaurant):
    _, client = admin_client

    response = client.get("/v1/admin/settings/")

    assert response.status_code == 200
    assert Decimal(str(response.data["gst_percentage"])) == restaurant.gst_percentage


def test_admin_can_update_gst_and_service_charge(admin_client, restaurant):
    _, client = admin_client

    response = client.patch(
        "/v1/admin/settings/", {"gst_percentage": "12.50", "service_charge_percentage": "2.00"}, format="json",
    )

    assert response.status_code == 200, response.data
    restaurant.refresh_from_db()
    assert restaurant.gst_percentage == Decimal("12.50")
    assert restaurant.service_charge_percentage == Decimal("2.00")


def test_manager_cannot_edit_gst_settings(manager_client):
    _, client = manager_client

    response = client.patch("/v1/admin/settings/", {"gst_percentage": "99.00"}, format="json")

    assert response.status_code == 403


def test_gst_settings_not_exposed_on_me_endpoint(admin_client):
    """Existing product decision — billing rates stay off /v1/auth/me/ for
    every staff role. This endpoint is the one deliberate exception, and
    it's Admin-only."""
    _, client = admin_client

    response = client.get("/v1/auth/me/")

    assert "gst_percentage" not in response.data["restaurant"]
