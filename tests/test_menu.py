import pytest

from apps.menu.models import PreparedPortion

pytestmark = pytest.mark.django_db


def test_customer_menu_hides_zero_portion_items(api_client, table, menu_item):
    portion = menu_item.prepared_portions.get()
    portion.portions_remaining = 0
    portion.save()

    response = api_client.get(f"/v1/menu/customer/{table.id}/")
    assert response.status_code == 200
    ids = [item["id"] for item in response.data]
    assert menu_item.id not in ids


def test_untracked_item_always_shows(api_client, table, menu_item):
    # A second item with no PreparedPortion row today is always available.
    from apps.menu.models import MenuItem

    untracked = MenuItem.objects.create(category=menu_item.category, name="Fries", price=80)

    response = api_client.get(f"/v1/menu/customer/{table.id}/")
    ids = [item["id"] for item in response.data]
    assert untracked.id in ids


def test_add_portions_increments_existing_row(manager_client, menu_item):
    _, client = manager_client
    portion = menu_item.prepared_portions.get()
    starting = portion.portions_remaining

    response = client.patch(f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 5}, format="json")
    assert response.status_code == 200

    portion.refresh_from_db()
    assert portion.portions_remaining == starting + 5
