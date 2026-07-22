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
