import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_missing_kds_key_rejected():
    client = APIClient()
    response = client.get("/v1/orders/active/")
    assert response.status_code in (401, 403)


def test_invalid_kds_key_rejected():
    client = APIClient()
    client.credentials(HTTP_X_KDS_API_KEY="not-a-real-key")
    response = client.get("/v1/orders/active/")
    assert response.status_code in (401, 403)


def test_inactive_kds_key_rejected(kds_device):
    kds_device.is_active = False
    kds_device.save()
    client = APIClient()
    client.credentials(HTTP_X_KDS_API_KEY=kds_device.api_key)
    response = client.get("/v1/orders/active/")
    assert response.status_code in (401, 403)


def test_valid_kds_key_accepted(kds_client):
    _, client = kds_client
    response = client.get("/v1/orders/active/")
    assert response.status_code == 200


def test_kds_device_me_confirms_valid_key(kds_client):
    device, client = kds_client
    response = client.get("/v1/kitchen/devices/me/")
    assert response.status_code == 200, response.data
    assert response.data["id"] == device.id
    assert response.data["api_key"] == device.api_key


def test_kds_device_me_rejects_invalid_key():
    client = APIClient()
    client.credentials(HTTP_X_KDS_API_KEY="not-a-real-key")
    response = client.get("/v1/kitchen/devices/me/")
    assert response.status_code in (401, 403)


def test_kds_device_me_updates_last_seen_at(kds_client):
    device, client = kds_client
    assert device.last_seen_at is None
    client.get("/v1/kitchen/devices/me/")
    device.refresh_from_db()
    assert device.last_seen_at is not None


def test_admin_can_create_kds_device_with_branch(admin_client, branch):
    _, client = admin_client

    response = client.post("/v1/kitchen/devices/", {"label": "Line 2", "branch": str(branch.id)}, format="json")

    assert response.status_code == 201, response.data
    assert response.data["branch"] == branch.id


def test_admin_can_rotate_kds_device_key(admin_client, kds_device):
    _, client = admin_client
    old_key = kds_device.api_key

    response = client.post(f"/v1/kitchen/devices/{kds_device.id}/rotate-key/")

    assert response.status_code == 200, response.data
    assert response.data["api_key"] != old_key
    kds_device.refresh_from_db()
    assert kds_device.api_key == response.data["api_key"]

    stale_client = APIClient()
    stale_client.credentials(HTTP_X_KDS_API_KEY=old_key)
    assert stale_client.get("/v1/orders/active/").status_code in (401, 403)

    fresh_client = APIClient()
    fresh_client.credentials(HTTP_X_KDS_API_KEY=kds_device.api_key)
    assert fresh_client.get("/v1/orders/active/").status_code == 200
