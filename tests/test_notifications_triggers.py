from decimal import Decimal

import pytest

from apps.inventory import services as inventory_services
from apps.inventory.models import Ingredient
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


def test_record_wastage_notifies_on_newly_low_stock(manager_client, admin_client, restaurant):
    _, manager = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Onions", unit="KG",
        current_stock=Decimal("10.00"), minimum_stock_level=Decimal("5.00"),
    )

    response = manager.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "6.00", "wastage_reason": "SPOILED"}, format="json",
    )

    assert response.status_code == 200, response.data
    assert Notification.objects.filter(recipient=admin_user, type="LOW_STOCK").exists()


def test_record_wastage_does_not_renotify_when_already_low(manager_client, admin_client, restaurant):
    _, manager = manager_client
    admin_user, _ = admin_client
    ingredient = Ingredient.objects.create(
        restaurant=restaurant, name="Garlic", unit="KG",
        current_stock=Decimal("3.00"), minimum_stock_level=Decimal("5.00"),
    )

    manager.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "1.00", "wastage_reason": "SPOILED"}, format="json",
    )

    assert Notification.objects.filter(recipient=admin_user, type="LOW_STOCK", data__ingredient_id=str(ingredient.id)).count() == 0


def test_create_staff_notifies_admins(admin_client, restaurant):
    admin_user, client = admin_client

    response = client.post(
        "/v1/staff/", {"email": "newhire@demo-bistro.demo", "name": "New Hire", "role": "SERVER"}, format="json",
    )

    assert response.status_code == 201, response.data
    assert Notification.objects.filter(recipient=admin_user, type="STAFF_ADDED").exists()


def test_add_portions_notifies_admins_and_managers(manager_client, admin_client, menu_item):
    _, manager = manager_client
    admin_user, _ = admin_client

    response = manager.patch(
        f"/v1/prepared-dishes/{menu_item.id}/add-portions/", {"additional_quantity": 5}, format="json",
    )

    assert response.status_code == 200, response.data
    assert Notification.objects.filter(recipient=admin_user, type="PREP_LOGGED").exists()
