import pytest

pytestmark = pytest.mark.django_db


def test_me_includes_own_tenants_feature_flags(admin_client, restaurant):
    _, client = admin_client

    response = client.get("/v1/auth/me/")

    assert response.status_code == 200
    assert response.data["restaurant"]["id"] == restaurant.id
    assert response.data["restaurant"]["slug"] == restaurant.slug
    assert response.data["restaurant"]["kitchen_enabled"] is True
    assert response.data["restaurant"]["billing_enabled"] is True


def test_me_reflects_flags_toggled_by_super_admin(admin_client, restaurant):
    _, client = admin_client
    restaurant.kitchen_enabled = False
    restaurant.save()

    response = client.get("/v1/auth/me/")

    assert response.data["restaurant"]["kitchen_enabled"] is False


def test_me_does_not_expose_billing_rates(admin_client):
    _, client = admin_client

    response = client.get("/v1/auth/me/")

    assert "gst_percentage" not in response.data["restaurant"]
    assert "service_charge_percentage" not in response.data["restaurant"]
