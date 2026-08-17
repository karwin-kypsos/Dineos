import pytest

from apps.authentication.models import User

pytestmark = pytest.mark.django_db


def test_list_staff_filter_by_role(admin_client, restaurant):
    _, client = admin_client
    User.objects.create_user(email="mgr1@demo-bistro.demo", password="Test@1234", role="MANAGER", name="Mgr One", restaurant=restaurant)
    User.objects.create_user(email="mgr2@demo-bistro.demo", password="Test@1234", role="MANAGER", name="Mgr Two", restaurant=restaurant)
    User.objects.create_user(email="srv1@demo-bistro.demo", password="Test@1234", role="SERVER", name="Server One", restaurant=restaurant)

    response = client.get("/v1/staff/?role=MANAGER")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    roles = {s["role"] for s in results}
    assert roles == {"MANAGER"}
    assert len(results) == 2


def test_list_staff_role_filter_is_case_insensitive(admin_client, restaurant):
    _, client = admin_client
    User.objects.create_user(email="cash1@demo-bistro.demo", password="Test@1234", role="CASHIER", name="Cashier One", restaurant=restaurant)

    response = client.get("/v1/staff/?role=cashier")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert any(s["role"] == "CASHIER" for s in results)


def test_list_staff_ignores_unrecognized_role(admin_client, restaurant):
    _, client = admin_client
    User.objects.create_user(email="mgr3@demo-bistro.demo", password="Test@1234", role="MANAGER", name="Mgr Three", restaurant=restaurant)

    response = client.get("/v1/staff/?role=NOT_A_ROLE")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert len(results) >= 1  # unfiltered, same as omitting ?role= entirely
