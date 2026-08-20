import pytest

pytestmark = pytest.mark.django_db


def test_kds_preflight_allows_custom_header(client):
    response = client.options(
        "/v1/kitchen/devices/me/",
        HTTP_ORIGIN="https://example.com",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="x-kds-api-key",
    )

    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == "https://example.com"
    assert "x-kds-api-key" in response["Access-Control-Allow-Headers"]
