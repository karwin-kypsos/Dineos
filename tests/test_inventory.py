from decimal import Decimal

import pytest

from apps.inventory.models import Ingredient, PurchaseOrder, StockMovement

pytestmark = pytest.mark.django_db


@pytest.fixture
def ingredient(restaurant):
    return Ingredient.objects.create(
        restaurant=restaurant, name="Chicken", unit="KG",
        current_stock=Decimal("10.00"), unit_cost=Decimal("200.00"), minimum_stock_level=Decimal("5.00"),
    )


def test_manager_can_create_ingredient(manager_client, restaurant):
    _, client = manager_client

    response = client.post(
        "/v1/inventory/ingredients/",
        {"name": "Rice", "unit": "KG", "minimum_stock_level": "10.00"}, format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["current_stock"] == "0.00"
    assert Ingredient.objects.filter(restaurant=restaurant, name="Rice").exists()


def test_server_cannot_create_ingredient(server_client):
    _, client = server_client

    response = client.post("/v1/inventory/ingredients/", {"name": "Rice", "unit": "KG"}, format="json")

    assert response.status_code == 403


def test_add_stock_increments_current_stock(manager_client, ingredient):
    _, client = manager_client

    response = client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/add-stock/", {"quantity": "5.00"}, format="json",
    )

    assert response.status_code == 200, response.data
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("15.00")
    assert StockMovement.objects.filter(ingredient=ingredient, movement_type="RESTOCK", quantity=Decimal("5.00")).exists()


def test_record_wastage_decrements_stock(manager_client, ingredient):
    _, client = manager_client

    response = client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "2.00", "wastage_reason": "SPOILED", "reason": "left out overnight"}, format="json",
    )

    assert response.status_code == 200, response.data
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("8.00")


def test_record_wastage_rejects_more_than_available(manager_client, ingredient):
    _, client = manager_client

    response = client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "999.00", "wastage_reason": "SPOILED"}, format="json",
    )

    assert response.status_code == 400
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("10.00")


def test_low_stock_filter(manager_client, restaurant, ingredient):
    _, client = manager_client
    Ingredient.objects.create(
        restaurant=restaurant, name="Onions", unit="KG",
        current_stock=Decimal("2.00"), minimum_stock_level=Decimal("5.00"),
    )

    response = client.get("/v1/inventory/ingredients/?low_stock=true")

    assert response.status_code == 200
    names = {i["name"] for i in response.data["results"]} if "results" in response.data else {i["name"] for i in response.data}
    assert names == {"Onions"}


def test_full_purchase_order_lifecycle(admin_client, manager_client, ingredient):
    admin, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"supplier_name": "Fresh Farms", "lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "20.00", "unit_cost": "210.00"}]},
        format="json",
    )
    assert create.status_code == 201, create.data
    po_id = create.data["id"]
    assert create.data["status"] == "PENDING"

    approve = admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/")
    assert approve.status_code == 200
    assert approve.data["status"] == "APPROVED"
    assert approve.data["approved_by_name"] == admin.name

    ordered = admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/mark-ordered/")
    assert ordered.status_code == 200
    assert ordered.data["status"] == "ORDERED"

    received = admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/receive/")
    assert received.status_code == 200
    assert received.data["status"] == "RECEIVED"
    assert received.data["lines"][0]["quantity_received"] == "20.00"

    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("30.00")  # 10 initial + 20 received


def test_cannot_receive_purchase_order_before_ordered(admin_client, manager_client, ingredient):
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    po_id = create.data["id"]

    response = admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/receive/")

    assert response.status_code == 409


def test_purchase_order_rejects_ingredient_from_other_restaurant(manager_client):
    _, client = manager_client

    from apps.restaurant.models import Restaurant

    foreign_restaurant = Restaurant.objects.create(name="Foreign Inv", slug="foreign-inventory")
    foreign_ingredient = Ingredient.objects.create(restaurant=foreign_restaurant, name="Butter", unit="KG")

    response = client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(foreign_ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )

    assert response.status_code == 404


def test_recipe_item_links_menu_item_to_ingredient(manager_client, menu_item, ingredient):
    _, client = manager_client

    response = client.post(
        "/v1/inventory/recipe-items/",
        {"menu_item": menu_item.id, "ingredient": str(ingredient.id), "quantity_per_serving": "0.250"},
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["ingredient_name"] == "Chicken"
