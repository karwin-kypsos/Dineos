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


def test_wastage_log_returns_todays_entries_with_cost_breakdown(manager_client, ingredient, restaurant):
    _, client = manager_client
    from apps.inventory.models import Ingredient

    milk = Ingredient.objects.create(
        restaurant=restaurant, name="Milk", unit="L", current_stock=Decimal("5.00"), unit_cost=Decimal("60.00"),
    )

    client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "2.00", "wastage_reason": "SPOILED", "reason": "left out overnight"}, format="json",
    )
    client.patch(
        f"/v1/inventory/ingredients/{milk.id}/record-wastage/",
        {"quantity": "1.00", "wastage_reason": "OVER_PREPPED"}, format="json",
    )

    response = client.get("/v1/inventory/wastage/")

    assert response.status_code == 200, response.data
    assert Decimal(str(response.data["total_cost"])) == Decimal("460.00")  # 2*200 + 1*60
    breakdown = response.data["breakdown_by_reason"]
    assert Decimal(str(breakdown["SPOILED"])) == Decimal("400.00")
    assert Decimal(str(breakdown["OVER_PREPPED"])) == Decimal("60.00")
    assert Decimal(str(breakdown["RETURNED"])) == Decimal("0")
    assert len(response.data["entries"]) == 2
    names = {e["ingredient_name"] for e in response.data["entries"]}
    assert names == {"Chicken", "Milk"}


def test_wastage_log_excludes_other_days(manager_client, ingredient):
    from datetime import timedelta

    from django.utils import timezone

    from apps.inventory import services as inventory_services
    from apps.inventory.models import StockMovement

    _, client = manager_client
    movement = inventory_services.record_wastage(ingredient.id, Decimal("1.00"), "SPOILED")
    StockMovement.objects.filter(id=movement.id).update(recorded_at=timezone.now() - timedelta(days=2))

    response = client.get("/v1/inventory/wastage/")

    assert response.status_code == 200
    assert response.data["entries"] == []
    assert Decimal(str(response.data["total_cost"])) == Decimal("0")


def test_wastage_log_rejects_bad_date_format(manager_client):
    _, client = manager_client

    response = client.get("/v1/inventory/wastage/?date=not-a-date")

    assert response.status_code == 400


def test_wastage_log_requires_admin_or_manager(server_client):
    _, client = server_client

    response = client.get("/v1/inventory/wastage/")

    assert response.status_code == 403


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


def test_list_ingredients_filter_by_branch(admin_client, restaurant):
    from apps.restaurant.models import Branch

    _, client = admin_client
    branch_a = Branch.objects.create(restaurant=restaurant, name="Branch A")
    branch_b = Branch.objects.create(restaurant=restaurant, name="Branch B")
    Ingredient.objects.create(restaurant=restaurant, branch=branch_a, name="A-only", unit="KG")
    Ingredient.objects.create(restaurant=restaurant, branch=branch_b, name="B-only", unit="KG")

    response = client.get(f"/v1/inventory/ingredients/?branch={branch_a.id}")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    names = {i["name"] for i in results}
    assert names == {"A-only"}


def test_ingredient_accepts_supplier_phone(manager_client, restaurant):
    _, client = manager_client

    response = client.post(
        "/v1/inventory/ingredients/",
        {"name": "Basil", "unit": "KG", "supplier_name": "Green Farms", "supplier_phone": "9876500000"},
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["supplier_phone"] == "9876500000"


def test_purchase_order_filter_by_status(manager_client, ingredient):
    _, client = manager_client
    client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )

    response = client.get("/v1/inventory/purchase-orders/?status=PENDING")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert all(po["status"] == "PENDING" for po in results)
    assert len(results) >= 1

    response = client.get("/v1/inventory/purchase-orders/?status=RECEIVED")
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert all(po["status"] == "RECEIVED" for po in results)


def test_purchase_order_needs_action_filter_and_branch_scoping(admin_client, ingredient, restaurant):
    from apps.authentication.serializers import DineOSTokenObtainPairSerializer
    from apps.authentication.models import User
    from apps.restaurant.models import Branch
    from rest_framework.test import APIClient

    admin, admin_c = admin_client
    branch_a = Branch.objects.create(restaurant=restaurant, name="Branch A")
    branch_b = Branch.objects.create(restaurant=restaurant, name="Branch B")

    ingredient_a = Ingredient.objects.create(
        restaurant=restaurant, branch=branch_a, name="Flour", unit="KG",
        current_stock=Decimal("10.00"), unit_cost=Decimal("50.00"), minimum_stock_level=Decimal("5.00"),
    )

    mgr_a = User.objects.create_user(email="mgr-a@test.dineos", password="Test@1234", role="MANAGER", name="Mgr A", restaurant=restaurant, branch=branch_a)
    token_a = DineOSTokenObtainPairSerializer.get_token(mgr_a)
    client_a = APIClient()
    client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {token_a.access_token}")

    first = client_a.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient_a.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    assert first.status_code == 201, first.data
    client_a.post(f"/v1/inventory/purchase-orders/{first.data['id']}/approve/")  # moves it out of PENDING

    second = client_a.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient_a.id), "quantity_ordered": "2.00"}]}, format="json",
    )
    assert second.status_code == 201, second.data

    response = admin_c.get("/v1/inventory/purchase-orders/?needs_action=true")
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert response.status_code == 200
    assert all(po["status"] == "PENDING" for po in results)
    assert len(results) == 1
    assert results[0]["id"] == second.data["id"]

    scoped_b = admin_c.get(f"/v1/inventory/purchase-orders/?needs_action=true&branch={branch_b.id}")
    scoped_b_results = scoped_b.data["results"] if isinstance(scoped_b.data, dict) else scoped_b.data
    assert scoped_b_results == []

    scoped_a = admin_c.get(f"/v1/inventory/purchase-orders/?needs_action=true&branch={branch_a.id}")
    scoped_a_results = scoped_a.data["results"] if isinstance(scoped_a.data, dict) else scoped_a.data
    assert len(scoped_a_results) == 1
    assert scoped_a_results[0]["id"] == second.data["id"]


def test_purchase_order_estimated_total_and_is_emergency_filter(manager_client, ingredient):
    _, client = manager_client
    normal = client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00", "unit_cost": "10.00"}]},
        format="json",
    )
    assert normal.status_code == 201, normal.data
    assert Decimal(str(normal.data["estimated_total"])) == Decimal("50.00")

    emergency = client.post(
        "/v1/inventory/purchase-orders/",
        {"is_emergency": True, "reason": "NOTICED",
         "lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "2.00", "unit_cost": "15.00"}]},
        format="json",
    )
    assert emergency.status_code == 201, emergency.data

    response = client.get("/v1/inventory/purchase-orders/?is_emergency=true")
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert all(po["is_emergency"] is True for po in results)
    assert any(po["id"] == emergency.data["id"] for po in results)
    assert not any(po["id"] == normal.data["id"] for po in results)


def test_purchase_order_search_by_ingredient_and_supplier(manager_client, ingredient):
    _, client = manager_client
    client.post(
        "/v1/inventory/purchase-orders/",
        {"supplier_name": "Dairy Fresh", "lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]},
        format="json",
    )

    by_ingredient = client.get("/v1/inventory/purchase-orders/?search=Chicken")
    results = by_ingredient.data["results"] if isinstance(by_ingredient.data, dict) else by_ingredient.data
    assert len(results) >= 1

    by_supplier = client.get("/v1/inventory/purchase-orders/?search=Dairy")
    results = by_supplier.data["results"] if isinstance(by_supplier.data, dict) else by_supplier.data
    assert len(results) >= 1

    no_match = client.get("/v1/inventory/purchase-orders/?search=NoSuchIngredientOrSupplier")
    results = no_match.data["results"] if isinstance(no_match.data, dict) else no_match.data
    assert results == []


def test_emergency_purchase_order_is_received_immediately_and_restocks(manager_client, ingredient):
    _, client = manager_client
    starting_stock = ingredient.current_stock

    response = client.post(
        "/v1/inventory/purchase-orders/",
        {
            "is_emergency": True, "reason": "AI_ALERT",
            "lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "3.00", "unit_cost": "210.00"}],
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["status"] == "RECEIVED"
    assert response.data["is_emergency"] is True
    assert response.data["reason"] == "AI_ALERT"
    assert response.data["lines"][0]["quantity_received"] == "3.00"

    ingredient.refresh_from_db()
    assert ingredient.current_stock == starting_stock + Decimal("3.00")


def test_non_emergency_purchase_order_stays_pending(manager_client, ingredient):
    _, client = manager_client
    starting_stock = ingredient.current_stock

    response = client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "3.00"}]}, format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["status"] == "PENDING"
    assert response.data["is_emergency"] is False

    ingredient.refresh_from_db()
    assert ingredient.current_stock == starting_stock  # unaffected until receive


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
