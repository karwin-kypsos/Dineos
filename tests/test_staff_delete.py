import pytest

from apps.authentication.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def server(restaurant):
    return User.objects.create_user(
        email="todelete@demo-bistro.demo", password="Test@1234", role="SERVER", name="To Delete", restaurant=restaurant,
    )


def test_delete_without_confirm_returns_409_and_does_not_delete(admin_client, server):
    _, client = admin_client

    response = client.delete(f"/v1/staff/{server.id}/")

    assert response.status_code == 409
    assert response.data["staff_member"]["email"] == "todelete@demo-bistro.demo"
    assert User.objects.filter(id=server.id).exists()


def test_delete_with_confirm_actually_deletes(admin_client, server):
    _, client = admin_client
    server_id = server.id

    response = client.delete(f"/v1/staff/{server_id}/?confirm=true")

    assert response.status_code == 204
    assert not User.objects.filter(id=server_id).exists()


def test_deactivate_is_unaffected_and_stays_soft(admin_client, server):
    _, client = admin_client

    response = client.patch(f"/v1/staff/{server.id}/deactivate/")

    assert response.status_code == 200
    server.refresh_from_db()
    assert server.is_active is False
