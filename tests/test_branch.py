import pytest

from apps.restaurant.models import Branch

pytestmark = pytest.mark.django_db


def test_admin_can_create_branch(admin_client, restaurant):
    _, client = admin_client

    response = client.post("/v1/branches/", {"name": "Downtown", "address": "12 Main St"}, format="json")

    assert response.status_code == 201, response.data
    assert response.data["name"] == "Downtown"
    assert response.data["is_active"] is True
    assert Branch.objects.filter(restaurant=restaurant, name="Downtown").exists()


def test_admin_can_list_only_own_restaurant_branches(admin_client, restaurant):
    _, client = admin_client
    Branch.objects.create(restaurant=restaurant, name="Own Branch")
    other_restaurant = restaurant.__class__.objects.create(name="Other", slug="other-restaurant")
    Branch.objects.create(restaurant=other_restaurant, name="Other Branch")

    response = client.get("/v1/branches/")

    assert response.status_code == 200
    names = {b["name"] for b in response.data["results"]} if "results" in response.data else {b["name"] for b in response.data}
    assert names == {"Own Branch"}


def test_non_admin_cannot_create_branch(manager_client):
    _, client = manager_client

    response = client.post("/v1/branches/", {"name": "Downtown"}, format="json")

    assert response.status_code == 403


def test_deleting_branch_soft_deactivates(admin_client, branch):
    _, client = admin_client

    response = client.delete(f"/v1/branches/{branch.id}/")

    assert response.status_code == 204
    branch.refresh_from_db()
    assert branch.is_active is False


def test_create_staff_with_branch(admin_client, branch):
    _, client = admin_client

    response = client.post(
        "/v1/staff/",
        {"email": "newserver@branch.test", "password": "Demo@1234", "role": "SERVER", "name": "New Server",
         "branch": str(branch.id)},
        format="json",
    )

    assert response.status_code == 201, response.data

    detail = client.get("/v1/staff/")
    created = next(s for s in detail.data["results"] if s["email"] == "newserver@branch.test") \
        if "results" in detail.data else next(s for s in detail.data if s["email"] == "newserver@branch.test")
    assert created["branch"]["id"] == str(branch.id)
    assert created["branch"]["name"] == branch.name


def test_create_staff_rejects_branch_from_other_restaurant(admin_client):
    _, client = admin_client

    from apps.restaurant.models import Restaurant
    foreign_restaurant = Restaurant.objects.create(name="Foreign", slug="foreign-restaurant")
    foreign_branch = Branch.objects.create(restaurant=foreign_restaurant, name="Foreign Branch")

    response = client.post(
        "/v1/staff/",
        {"email": "cross@branch.test", "password": "Demo@1234", "role": "SERVER", "name": "Cross",
         "branch": str(foreign_branch.id)},
        format="json",
    )

    assert response.status_code == 400
    assert "branch" in response.data


def test_staff_creation_without_branch_still_works(admin_client):
    _, client = admin_client

    response = client.post(
        "/v1/staff/",
        {"email": "nobranch@branch.test", "password": "Demo@1234", "role": "SERVER", "name": "No Branch"},
        format="json",
    )

    assert response.status_code == 201, response.data


def test_create_branch_with_photo_and_manager(admin_client, manager_client):
    _, client = admin_client
    manager_user, _ = manager_client

    response = client.post(
        "/v1/branches/",
        {"name": "Kochi", "address": "MG Road", "photo_url": "https://example.com/kochi.jpg",
         "manager": str(manager_user.id)},
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["photo_url"] == "https://example.com/kochi.jpg"
    assert str(response.data["manager"]) == str(manager_user.id)
    assert response.data["manager_detail"]["name"] == manager_user.name


def test_branch_rejects_manager_with_wrong_role(admin_client, server_client):
    _, client = admin_client
    server_user, _ = server_client

    response = client.post(
        "/v1/branches/", {"name": "Trivandrum", "manager": str(server_user.id)}, format="json",
    )

    assert response.status_code == 400
    assert "manager" in response.data


def test_branch_rejects_manager_from_other_restaurant(admin_client):
    _, client = admin_client

    from apps.authentication.models import User
    from apps.restaurant.models import Restaurant

    foreign_restaurant = Restaurant.objects.create(name="Foreign2", slug="foreign-restaurant-2")
    foreign_manager = User.objects.create_user(
        email="foreignmgr@branch.test", password="Demo@1234", role="MANAGER",
        name="Foreign Manager", restaurant=foreign_restaurant,
    )

    response = client.post(
        "/v1/branches/", {"name": "Kozhikode", "manager": str(foreign_manager.id)}, format="json",
    )

    assert response.status_code == 400
    assert "manager" in response.data
