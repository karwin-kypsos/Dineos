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


def test_admin_can_create_ingredient_for_a_specific_branch(admin_client, restaurant, branch):
    """2026-09-03 - found while rebuilding test data: an Admin (no fixed
    branch of their own) specifying a branch in the request body used to
    have it silently discarded and replaced with None."""
    _, client = admin_client

    response = client.post(
        "/v1/inventory/ingredients/", {"name": "Rice", "unit": "KG", "branch": str(branch.id)}, format="json",
    )

    assert response.status_code == 201, response.data
    assert str(response.data["branch"]) == str(branch.id)


def test_admin_can_create_same_ingredient_name_on_two_different_branches(admin_client, restaurant):
    from apps.restaurant.models import Branch

    branch_a = Branch.objects.create(restaurant=restaurant, name="Branch A")
    branch_b = Branch.objects.create(restaurant=restaurant, name="Branch B")
    _, client = admin_client

    r1 = client.post("/v1/inventory/ingredients/", {"name": "Rice", "unit": "KG", "branch": str(branch_a.id)}, format="json")
    r2 = client.post("/v1/inventory/ingredients/", {"name": "Rice", "unit": "KG", "branch": str(branch_b.id)}, format="json")

    assert r1.status_code == 201, r1.data
    assert r2.status_code == 201, r2.data  # different branches - not a real conflict


def test_manager_cannot_create_ingredient_for_another_branch(manager_client, restaurant, branch):
    from apps.restaurant.models import Branch

    other_branch = Branch.objects.create(restaurant=restaurant, name="Other Branch")
    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])

    response = client.post(
        "/v1/inventory/ingredients/", {"name": "Rice", "unit": "KG", "branch": str(other_branch.id)}, format="json",
    )

    assert response.status_code == 201, response.data
    assert str(response.data["branch"]) == str(branch.id)  # forced to their own, not other_branch


def test_ingredient_branch_rejects_another_restaurants_branch(admin_client):
    from apps.restaurant.models import Branch, Restaurant

    other_restaurant = Restaurant.objects.create(name="Other Restaurant", slug="other-restaurant-ing")
    foreign_branch = Branch.objects.create(restaurant=other_restaurant, name="Foreign Branch")
    _, client = admin_client

    response = client.post(
        "/v1/inventory/ingredients/", {"name": "Rice", "unit": "KG", "branch": str(foreign_branch.id)}, format="json",
    )

    assert response.status_code == 400


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
    assert all(po["status"] == "PENDING_APPROVAL" for po in results)
    assert len(results) >= 1

    # The enum was renamed on 2026-09-21. An app still sending the old
    # value must keep getting the right rows, not every row - an
    # unrecognised status falls through the filter entirely.
    response = client.get("/v1/inventory/purchase-orders/?status=RECEIVED")
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert all(po["status"] == "FULLY_RECEIVED" for po in results)

    response = client.get("/v1/inventory/purchase-orders/?status=PENDING_APPROVAL")
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert all(po["status"] == "PENDING_APPROVAL" for po in results)
    assert len(results) >= 1


def test_purchase_order_date_range_filters_inclusive(manager_client, ingredient):
    """New: date_from/date_to (2026-08-27, per Shereena's Purchase Order
    History screen needing a from/to range, same as Billing/bill history)."""
    from datetime import timedelta

    from django.utils import timezone

    _, client = manager_client
    in_range = client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    ).data
    po_in_range = PurchaseOrder.objects.get(id=in_range["id"])
    po_in_range.created_at = po_in_range.created_at - timedelta(days=3)
    po_in_range.save(update_fields=["created_at"])

    outside_range = client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    ).data
    po_outside_range = PurchaseOrder.objects.get(id=outside_range["id"])
    po_outside_range.created_at = po_outside_range.created_at - timedelta(days=10)
    po_outside_range.save(update_fields=["created_at"])

    today = timezone.localdate()
    date_from = (today - timedelta(days=5)).isoformat()
    date_to = today.isoformat()

    response = client.get(f"/v1/inventory/purchase-orders/?date_from={date_from}&date_to={date_to}")

    assert response.status_code == 200
    ids = {po["id"] for po in response.data["results"]}
    assert str(po_in_range.id) in ids
    assert str(po_outside_range.id) not in ids


def test_purchase_order_single_date_filter(manager_client, ingredient):
    """New: date (2026-09-04, per Karwin - Admin's Purchase Orders list
    always sends one selected branch + one selected date). Exact-day match
    on created_at, distinct from the date_from/date_to range above."""
    from datetime import timedelta

    from django.utils import timezone

    _, client = manager_client
    today_po = client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    ).data

    yesterday_po = client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    ).data
    po_yesterday = PurchaseOrder.objects.get(id=yesterday_po["id"])
    po_yesterday.created_at = po_yesterday.created_at - timedelta(days=1)
    po_yesterday.save(update_fields=["created_at"])

    today = timezone.localdate().isoformat()
    response = client.get(f"/v1/inventory/purchase-orders/?date={today}")

    assert response.status_code == 200
    ids = {po["id"] for po in response.data["results"]}
    assert today_po["id"] in ids
    assert yesterday_po["id"] not in ids


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
    assert all(po["status"] == "PENDING_APPROVAL" for po in results)
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
    assert response.data["status"] == "FULLY_RECEIVED"
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
    assert response.data["status"] == "PENDING_APPROVAL"
    assert response.data["is_emergency"] is False

    ingredient.refresh_from_db()
    assert ingredient.current_stock == starting_stock  # unaffected until receive


def test_full_purchase_order_lifecycle(admin_client, manager_client, ingredient):
    """2026-09-21, rewritten for the goods-receipt flow: raise, approve,
    receive. There is no longer a mark-ordered step."""
    admin, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"supplier_name": "Fresh Farms",
         "lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "20.00", "unit_cost": "210.00"}]},
        format="json",
    )
    assert create.status_code == 201, create.data
    po_id = create.data["id"]
    assert create.data["status"] == "PENDING_APPROVAL"
    line_id = create.data["lines"][0]["id"]

    approve = admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")
    assert approve.status_code == 200, approve.data
    assert approve.data["status"] == "APPROVED"
    assert approve.data["approved_by_name"] == admin.name
    # Approving with no body approves everything at the requested amount.
    assert approve.data["lines"][0]["approved_quantity"] == "20.00"

    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("10.00"), "approval must not move stock"

    receipt = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "20.00"}]}, format="json",
    )
    assert receipt.status_code == 201, receipt.data
    assert receipt.data["lines"][0]["received_quantity"] == "20.00"

    detail = admin_c.get(f"/v1/inventory/purchase-orders/{po_id}/")
    assert detail.data["status"] == "FULLY_RECEIVED"
    assert detail.data["lines"][0]["quantity_received"] == "20.00"
    assert len(detail.data["goods_receipts"]) == 1

    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("30.00")  # 10 initial + 20 received


def test_partial_deliveries_accumulate_and_then_complete(admin_client, manager_client, ingredient):
    """The whole point of the rewrite: a supplier who short-ships and
    sends the rest later produces two receipts against one PO, and the
    line's received quantity accumulates rather than being overwritten."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "10.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")

    first = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "4.00"}]}, format="json",
    )
    assert first.status_code == 201
    mid = admin_c.get(f"/v1/inventory/purchase-orders/{po_id}/")
    assert mid.data["status"] == "PARTIALLY_RECEIVED"
    assert mid.data["lines"][0]["quantity_received"] == "4.00"
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("14.00")

    second = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "6.00"}]}, format="json",
    )
    assert second.status_code == 201
    done = admin_c.get(f"/v1/inventory/purchase-orders/{po_id}/")
    assert done.data["status"] == "FULLY_RECEIVED"
    assert done.data["lines"][0]["quantity_received"] == "10.00", "cumulative, not overwritten"
    assert len(done.data["goods_receipts"]) == 2
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("20.00")


def test_approving_less_than_requested_caps_what_can_be_received(admin_client, manager_client, ingredient):
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "10.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]

    approve = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/approve/",
        {"items": [{"line_id": line_id, "approved_quantity": "6.00"}], "note": "supplier short"},
        format="json",
    )
    assert approve.status_code == 200, approve.data
    assert approve.data["lines"][0]["approved_quantity"] == "6.00"
    assert approve.data["approval_note"] == "supplier short"

    # 6 is fine; the 7th unit is an over-delivery against the APPROVED
    # amount even though 10 was originally requested.
    ok = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "6.00"}]}, format="json",
    )
    assert ok.status_code == 201
    over = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "1.00"}]}, format="json",
    )
    assert over.status_code == 409
    assert over.data["requires_confirmation"] is True


def test_over_delivery_is_refused_then_accepted_on_confirmation(admin_client, manager_client, ingredient):
    """Never silently clamped, never silently accepted."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")

    refused = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "8.00"}]}, format="json",
    )
    assert refused.status_code == 409
    assert refused.data["requires_confirmation"] is True
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("10.00"), "a refused receipt must not move stock"

    accepted = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "8.00"}], "confirm_overdelivery": True},
        format="json",
    )
    assert accepted.status_code == 201
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("18.00"), "the full 8 is taken, not clamped to 5"


def test_short_shipped_po_can_be_closed_with_a_reason(admin_client, manager_client, ingredient):
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "10.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")

    # Cannot close before anything has been received.
    too_early = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/close/", {"reason": "giving up"}, format="json")
    assert too_early.status_code == 409

    admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "4.00"}]}, format="json",
    )
    closed = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/close/",
        {"reason": "supplier cannot source the rest"}, format="json",
    )
    assert closed.status_code == 200, closed.data
    assert closed.data["status"] == "CLOSED"
    assert closed.data["closed_reason"] == "supplier cannot source the rest"


def test_discrepancy_report_shows_ordered_approved_received(admin_client, manager_client, ingredient):
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "10.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]
    admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/approve/",
        {"items": [{"line_id": line_id, "approved_quantity": "8.00"}]}, format="json",
    )
    admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "3.00"}]}, format="json",
    )

    report = admin_c.get(f"/v1/inventory/purchase-orders/{po_id}/discrepancy/")
    assert report.status_code == 200, report.data
    row = report.data["lines"][0]
    assert row["quantity_ordered"] == Decimal("10.00")
    assert row["approved_quantity"] == Decimal("8.00")
    assert row["quantity_received"] == Decimal("3.00")
    assert row["outstanding"] == Decimal("5.00")
    assert row["over_received"] == Decimal("0")
    assert report.data["fully_satisfied"] is False


def test_emergency_purchase_goes_through_a_real_goods_receipt(manager_client, ingredient):
    """The single-stock-path rule has no exceptions. An emergency
    purchase still restocks immediately, but it does so by creating a
    genuine goods receipt rather than writing stock directly."""
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"is_emergency": True,
         "lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "7.00"}]},
        format="json",
    )
    assert create.status_code == 201, create.data
    po_id = create.data["id"]

    detail = manager_c.get(f"/v1/inventory/purchase-orders/{po_id}/")
    assert detail.data["status"] == "FULLY_RECEIVED"
    assert len(detail.data["goods_receipts"]) == 1, "the restock must be a real receipt, not a bare stock write"
    assert detail.data["goods_receipts"][0]["lines"][0]["received_quantity"] == "7.00"
    assert detail.data["lines"][0]["approved_quantity"] == "7.00"

    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("17.00")


def test_stock_cannot_be_received_before_approval(admin_client, manager_client, ingredient):
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]

    response = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "5.00"}]}, format="json",
    )

    assert response.status_code == 409
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("10.00")


def test_goods_receipt_rejects_a_line_from_another_purchase_order(admin_client, manager_client, ingredient):
    _, admin_c = admin_client
    _, manager_c = manager_client

    a = manager_c.post("/v1/inventory/purchase-orders/",
                       {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json")
    b = manager_c.post("/v1/inventory/purchase-orders/",
                       {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json")
    admin_c.post(f"/v1/inventory/purchase-orders/{a.data['id']}/approve/", {}, format="json")

    response = admin_c.post(
        f"/v1/inventory/purchase-orders/{a.data['id']}/goods-receipts/",
        {"items": [{"line_id": b.data["lines"][0]["id"], "received_quantity": "1.00"}]}, format="json",
    )
    assert response.status_code == 404


def test_goods_receipt_is_tagged_as_such_in_stock_history(admin_client, manager_client, ingredient):
    """A delivery and a hand-typed correction both raise stock. The
    movement row has to say which it was, or the audit trail is useless."""
    from apps.inventory.models import StockMovement

    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")
    admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "5.00"}]}, format="json",
    )

    movement = StockMovement.objects.filter(
        ingredient=ingredient, movement_type=StockMovement.MovementType.RESTOCK,
    ).latest("recorded_at")
    assert movement.adjustment_reason == StockMovement.AdjustmentReason.GOODS_RECEIPT


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


def test_ingredient_list_excludes_branch_less_legacy_rows_for_a_branched_user(
    manager_client, restaurant, branch
):
    """2026-09-21, per Karwin: the branch-less-is-shared fallback is gone
    from Inventory too, the same way it left the Menu endpoints on
    2026-09-14. An ingredient left with branch=None predates Branch
    existing, and was showing up in every branch's stock list at once."""
    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])

    legacy = Ingredient.objects.create(
        restaurant=restaurant, branch=None, name="Legacy Flour", unit="KG",
        current_stock=Decimal("5.00"), unit_cost=Decimal("40.00"), minimum_stock_level=Decimal("1.00"),
    )
    own = Ingredient.objects.create(
        restaurant=restaurant, branch=branch, name="Branch Flour", unit="KG",
        current_stock=Decimal("5.00"), unit_cost=Decimal("40.00"), minimum_stock_level=Decimal("1.00"),
    )

    response = client.get("/v1/inventory/ingredients/")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    names = {r["id"] for r in results}
    assert str(own.id) in names
    assert str(legacy.id) not in names


def test_branch_less_ingredients_stay_visible_to_a_branch_less_admin(admin_client, restaurant):
    """The other half: strays must not become unreachable. An Admin has no
    branch, so the scoping is skipped and they can still find and reassign
    a pre-Branch ingredient."""
    _, client = admin_client
    legacy = Ingredient.objects.create(
        restaurant=restaurant, branch=None, name="Legacy Salt", unit="KG",
        current_stock=Decimal("5.00"), unit_cost=Decimal("10.00"), minimum_stock_level=Decimal("1.00"),
    )

    response = client.get("/v1/inventory/ingredients/")

    assert response.status_code == 200
    results = response.data["results"] if isinstance(response.data, dict) else response.data
    assert str(legacy.id) in {r["id"] for r in results}


def test_wastage_report_excludes_branch_less_ingredients_for_a_branched_user(
    manager_client, restaurant, branch
):
    """Same strictness on the wastage report, which scopes through the
    ingredient's branch rather than its own."""
    from django.utils import timezone

    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])

    legacy = Ingredient.objects.create(
        restaurant=restaurant, branch=None, name="Legacy Oil", unit="L",
        current_stock=Decimal("9.00"), unit_cost=Decimal("100.00"), minimum_stock_level=Decimal("1.00"),
    )
    own = Ingredient.objects.create(
        restaurant=restaurant, branch=branch, name="Branch Oil", unit="L",
        current_stock=Decimal("9.00"), unit_cost=Decimal("100.00"), minimum_stock_level=Decimal("1.00"),
    )
    for ing in (legacy, own):
        StockMovement.objects.create(
            ingredient=ing, movement_type=StockMovement.MovementType.WASTAGE,
            quantity=Decimal("1.00"), unit_cost_at_time=Decimal("100.00"),
            wastage_reason=StockMovement.WastageReason.SPOILED,
            recorded_at=timezone.now(),
        )

    response = client.get("/v1/inventory/wastage/")

    assert response.status_code == 200, response.data
    names = {e["ingredient_name"] for e in response.data["entries"]}
    assert "Branch Oil" in names
    assert "Legacy Oil" not in names
