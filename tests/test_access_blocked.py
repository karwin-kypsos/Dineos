"""A SUSPENDED organization's 403 carries code "access_blocked" (2026-10-12,
per Karwin), so the app can tell it apart from an ordinary permission 403
and from PAYMENT_DUE's "subscription_required"."""
import pytest

pytestmark = pytest.mark.django_db


def _set_status(restaurant, status):
    restaurant.status = status
    restaurant.save(update_fields=["status"])


def test_suspended_staff_token_gets_access_blocked(admin_client, restaurant):
    _, client = admin_client
    _set_status(restaurant, "SUSPENDED")

    response = client.get("/v1/branches/")

    assert response.status_code == 403
    assert response.json() == {"detail": "This organization's account has been suspended.", "code": "access_blocked"}


def test_suspended_kitchen_tablet_gets_access_blocked(kds_client, restaurant):
    _, client = kds_client
    _set_status(restaurant, "SUSPENDED")

    response = client.get("/v1/kitchen/devices/me/")

    assert response.status_code == 403 and response.json()["code"] == "access_blocked"


def test_payment_due_keeps_subscription_required(manager_client, restaurant):
    _, client = manager_client
    _set_status(restaurant, "PAYMENT_DUE")

    assert client.get("/v1/branches/").json()["code"] == "subscription_required"


def test_an_ordinary_permission_403_has_no_code(manager_client):
    _, client = manager_client

    response = client.get("/v1/subscriptions/current/")  # Admin only

    assert response.status_code == 403 and "code" not in response.json()
