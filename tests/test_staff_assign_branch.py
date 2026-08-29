import pytest

from apps.authentication.models import User
from apps.restaurant.models import Branch

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(restaurant):
    return User.objects.create_user(
        email="manager.nobranch@demo-bistro.demo", password="Test@1234", role="MANAGER",
        name="No Branch Manager", restaurant=restaurant,
    )


def test_assign_branch_sets_branch(admin_client, manager, branch):
    _, client = admin_client
    assert manager.branch is None

    response = client.patch(f"/v1/staff/{manager.id}/assign-branch/", {"branch": str(branch.id)}, format="json")

    assert response.status_code == 200
    manager.refresh_from_db()
    assert manager.branch_id == branch.id
    assert response.data["branch"]["id"] == str(branch.id)


def test_assign_branch_can_move_to_a_different_branch(admin_client, restaurant, manager, branch):
    _, client = admin_client
    other_branch = Branch.objects.create(restaurant=restaurant, name="Other Branch")
    manager.branch = branch
    manager.save(update_fields=["branch"])

    response = client.patch(f"/v1/staff/{manager.id}/assign-branch/", {"branch": str(other_branch.id)}, format="json")

    assert response.status_code == 200
    manager.refresh_from_db()
    assert manager.branch_id == other_branch.id


def test_assign_branch_null_unassigns(admin_client, manager, branch):
    _, client = admin_client
    manager.branch = branch
    manager.save(update_fields=["branch"])

    response = client.patch(f"/v1/staff/{manager.id}/assign-branch/", {"branch": None}, format="json")

    assert response.status_code == 200
    manager.refresh_from_db()
    assert manager.branch is None


def test_assign_branch_missing_field_returns_400(admin_client, manager):
    _, client = admin_client

    response = client.patch(f"/v1/staff/{manager.id}/assign-branch/", {}, format="json")

    assert response.status_code == 400


def test_assign_branch_rejects_branch_from_another_restaurant(admin_client, manager):
    from apps.restaurant.models import Restaurant

    other_restaurant = Restaurant.objects.create(name="Other Restaurant", slug="other-restaurant")
    foreign_branch = Branch.objects.create(restaurant=other_restaurant, name="Foreign Branch")

    _, client = admin_client
    response = client.patch(f"/v1/staff/{manager.id}/assign-branch/", {"branch": str(foreign_branch.id)}, format="json")

    assert response.status_code == 404
    manager.refresh_from_db()
    assert manager.branch is None


def test_assign_branch_non_admin_forbidden(manager_client, manager, branch):
    _, client = manager_client

    response = client.patch(f"/v1/staff/{manager.id}/assign-branch/", {"branch": str(branch.id)}, format="json")

    assert response.status_code == 403
