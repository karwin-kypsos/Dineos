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
        f"/v1/inventory/ingredients/{ingredient.id}/add-stock/",
        {"quantity": "5.00", "adjustment_reason": "STOCK_COUNT_CORRECTION"}, format="json",
    )

    assert response.status_code == 200, response.data
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("15.00")
    movement = StockMovement.objects.filter(
        ingredient=ingredient, movement_type="RESTOCK", quantity=Decimal("5.00"),
    ).latest("recorded_at")
    assert movement.adjustment_reason == "STOCK_COUNT_CORRECTION"


def test_add_stock_now_requires_a_reason(manager_client, ingredient):
    """2026-09-21, per the goods-receipt spec: a manual stock-in must say
    why. A hand-typed correction and a real delivery both raise stock,
    and without a reason on the row they cannot be told apart afterwards
    - which is the audit gap the whole feature exists to close.

    This is a BREAKING change to the endpoint: a caller that omits it
    now gets 400 rather than silently recording an unlabelled restock.
    """
    _, client = manager_client

    response = client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/add-stock/", {"quantity": "5.00"}, format="json",
    )

    assert response.status_code == 400
    assert "adjustment_reason" in response.data
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("10.00"), "a rejected call must not move stock"


def test_add_stock_cannot_masquerade_as_a_goods_receipt(manager_client, ingredient):
    """GOODS_RECEIPT is set by record_goods_receipt itself and is not on
    offer here - otherwise a manual edit could dress itself up as a real
    delivery, which defeats the point of tagging them apart."""
    _, client = manager_client

    response = client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/add-stock/",
        {"quantity": "5.00", "adjustment_reason": "GOODS_RECEIPT"}, format="json",
    )

    assert response.status_code == 400
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("10.00")


def test_every_manual_reason_is_accepted(manager_client, ingredient):
    _, client = manager_client
    for reason in ("STOCK_COUNT_CORRECTION", "WASTAGE", "OPENING_STOCK"):
        response = client.patch(
            f"/v1/inventory/ingredients/{ingredient.id}/add-stock/",
            {"quantity": "1.00", "adjustment_reason": reason}, format="json",
        )
        assert response.status_code == 200, (reason, response.data)


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
        {"items": [{"item_id": line_id, "received_quantity": "20.00"}]}, format="json",
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
        {"items": [{"item_id": line_id, "received_quantity": "4.00"}]}, format="json",
    )
    assert first.status_code == 201
    mid = admin_c.get(f"/v1/inventory/purchase-orders/{po_id}/")
    assert mid.data["status"] == "PARTIALLY_RECEIVED"
    assert mid.data["lines"][0]["quantity_received"] == "4.00"
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("14.00")

    second = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": line_id, "received_quantity": "6.00"}]}, format="json",
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
        {"items": [{"item_id": line_id, "approved_quantity": "6.00"}], "note": "supplier short"},
        format="json",
    )
    assert approve.status_code == 200, approve.data
    assert approve.data["lines"][0]["approved_quantity"] == "6.00"
    assert approve.data["approval_note"] == "supplier short"

    # 6 is fine; the 7th unit is an over-delivery against the APPROVED
    # amount even though 10 was originally requested.
    ok = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": line_id, "received_quantity": "6.00"}]}, format="json",
    )
    assert ok.status_code == 201
    over = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": line_id, "received_quantity": "1.00"}]}, format="json",
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
        {"items": [{"item_id": line_id, "received_quantity": "8.00"}]}, format="json",
    )
    assert refused.status_code == 409
    assert refused.data["requires_confirmation"] is True
    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("10.00"), "a refused receipt must not move stock"

    accepted = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": line_id, "received_quantity": "8.00"}], "confirm_overdelivery": True},
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
        {"items": [{"item_id": line_id, "received_quantity": "4.00"}]}, format="json",
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
        {"items": [{"item_id": line_id, "approved_quantity": "8.00"}]}, format="json",
    )
    admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": line_id, "received_quantity": "3.00"}]}, format="json",
    )

    report = admin_c.get(f"/v1/inventory/purchase-orders/{po_id}/discrepancy/")
    assert report.status_code == 200, report.data
    row = report.data["lines"][0]
    # Decimal STRINGS, matching the PO line serializer and every other
    # quantity in this API - a Decimal in a plain dict renders as a JSON
    # float, which had this endpoint saying 20.0 where the PO itself says
    # "20.00" for the same value.
    assert row["quantity_ordered"] == "10.00"
    assert row["approved_quantity"] == "8.00"
    assert row["quantity_received"] == "3.00"
    assert row["outstanding"] == "5.00"
    assert row["over_received"] == "0.00"
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
        {"items": [{"item_id": line_id, "received_quantity": "5.00"}]}, format="json",
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
        {"items": [{"item_id": b.data["lines"][0]["id"], "received_quantity": "1.00"}]}, format="json",
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
        {"items": [{"item_id": line_id, "received_quantity": "5.00"}]}, format="json",
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


def test_approve_and_receive_accept_the_spec_name_item_id(admin_client, manager_client, ingredient):
    """2026-09-21: the spec calls this item_id and that is canonical -
    the Flutter models are built against it."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    po_id = create.data["id"]
    item_id = create.data["lines"][0]["id"]

    approve = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/approve/",
        {"items": [{"item_id": item_id, "approved_quantity": "5.00"}]}, format="json",
    )
    assert approve.status_code == 200, approve.data

    receipt = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": item_id, "received_quantity": "5.00"}]}, format="json",
    )
    assert receipt.status_code == 201, receipt.data


def test_line_id_still_works_as_an_alias(admin_client, manager_client, ingredient):
    """The first live payloads I sent the frontend used line_id. Silently
    breaking something already handed over is worse than one alias."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]

    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")
    receipt = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"line_id": line_id, "received_quantity": "5.00"}]}, format="json",
    )
    assert receipt.status_code == 201, receipt.data


def test_a_line_reference_with_neither_name_is_rejected(admin_client, manager_client, ingredient):
    """Neither given is an error, not a guess."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    po_id = create.data["id"]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")

    response = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"received_quantity": "5.00"}]}, format="json",
    )
    assert response.status_code == 400


def test_conflicting_item_id_and_line_id_is_rejected(admin_client, manager_client, ingredient):
    """Both given but disagreeing is ambiguous - refuse rather than pick."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "5.00"}]}, format="json",
    )
    po_id = create.data["id"]
    line_id = create.data["lines"][0]["id"]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")

    response = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": line_id, "line_id": line_id + 999, "received_quantity": "5.00"}]},
        format="json",
    )
    assert response.status_code == 400


def test_stock_additions_lists_manual_adds_only(admin_client, manager_client, ingredient, branch):
    """2026-09-22, per Karwin: the audit list is the mirror of the wastage
    log. Goods-receipt additions are deliberately excluded - those
    already have full traceability through the purchase order, and
    mixing them in would bury the hand-entered ones this exists to
    surface."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    manager_c.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/add-stock/",
        {"quantity": "4.00", "unit_cost": "12.00", "adjustment_reason": "STOCK_COUNT_CORRECTION"},
        format="json",
    )

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "6.00"}]}, format="json",
    )
    po_id, item_id = create.data["id"], create.data["lines"][0]["id"]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")
    admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": item_id, "received_quantity": "6.00"}]}, format="json",
    )

    response = admin_c.get("/v1/inventory/stock-additions/")

    assert response.status_code == 200, response.data
    assert "count" in response.data and "results" in response.data, "must paginate like other lists"
    rows = response.data["results"]
    quantities = [r["quantity"] for r in rows]
    assert "4.00" in quantities, "the manual add must be listed"
    assert "6.00" not in quantities, "the goods-receipt add must NOT be listed"

    row = [r for r in rows if r["quantity"] == "4.00"][0]
    assert row["ingredient_name"] == ingredient.name
    assert row["unit"] == ingredient.unit
    assert row["reason"] == "STOCK_COUNT_CORRECTION"
    assert row["unit_cost"] == "12.00"
    assert row["performed_by_name"] is not None
    assert "created_at" in row


def test_stock_additions_filters(admin_client, manager_client, ingredient, restaurant):
    from datetime import timedelta

    from django.utils import timezone

    from apps.inventory.models import Ingredient

    _, admin_c = admin_client
    _, manager_c = manager_client
    other = Ingredient.objects.create(
        restaurant=restaurant, name="Filter Probe", unit="L",
        current_stock=Decimal("1.00"), minimum_stock_level=Decimal("1.00"),
    )

    manager_c.patch(f"/v1/inventory/ingredients/{ingredient.id}/add-stock/",
                    {"quantity": "2.00", "adjustment_reason": "OPENING_STOCK"}, format="json")
    manager_c.patch(f"/v1/inventory/ingredients/{other.id}/add-stock/",
                    {"quantity": "3.00", "adjustment_reason": "STOCK_COUNT_CORRECTION"}, format="json")

    by_reason = admin_c.get("/v1/inventory/stock-additions/?reason=OPENING_STOCK")
    assert {r["reason"] for r in by_reason.data["results"]} == {"OPENING_STOCK"}

    by_ingredient = admin_c.get(f"/v1/inventory/stock-additions/?ingredient={other.id}")
    # Response.data holds the real UUID object - it only becomes a
    # string once rendered to JSON, so compare as UUIDs here.
    assert {str(r["ingredient"]) for r in by_ingredient.data["results"]} == {str(other.id)}

    today = timezone.localdate().isoformat()
    in_range = admin_c.get(f"/v1/inventory/stock-additions/?date_from={today}&date_to={today}")
    assert len(in_range.data["results"]) >= 2

    past = (timezone.localdate() - timedelta(days=5)).isoformat()
    out_of_range = admin_c.get(f"/v1/inventory/stock-additions/?date_from={past}&date_to={past}")
    assert out_of_range.data["results"] == []

    # A malformed ingredient id returns nothing rather than everything -
    # the failure mode that matters on an audit endpoint.
    assert admin_c.get("/v1/inventory/stock-additions/?ingredient=not-a-uuid").data["results"] == []


def test_stock_additions_are_branch_scoped(manager_client, restaurant, branch, ingredient):
    """Same scoping as the rest of inventory since 2026-09-21: a user with
    a branch sees only their own branch's rows."""
    from apps.inventory.models import Ingredient
    from apps.restaurant.models import Branch

    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])

    other_branch = Branch.objects.create(restaurant=restaurant, name="Far Branch")
    mine = Ingredient.objects.create(
        restaurant=restaurant, branch=branch, name="Mine", unit="KG",
        current_stock=Decimal("1.00"), minimum_stock_level=Decimal("1.00"),
    )
    theirs = Ingredient.objects.create(
        restaurant=restaurant, branch=other_branch, name="Theirs", unit="KG",
        current_stock=Decimal("1.00"), minimum_stock_level=Decimal("1.00"),
    )
    from apps.inventory.services import add_stock

    add_stock(mine.id, Decimal("1.00"), adjustment_reason="OPENING_STOCK")
    add_stock(theirs.id, Decimal("1.00"), adjustment_reason="OPENING_STOCK")

    names = {r["ingredient_name"] for r in client.get("/v1/inventory/stock-additions/").data["results"]}
    assert "Mine" in names
    assert "Theirs" not in names


def test_estimated_total_is_a_decimal_string_like_every_other_money_field(manager_client, ingredient):
    """2026-09-22. estimated_total came off the wire as a bare float
    (2100.0) while the same response reported quantity_ordered as
    "20.00" - one object, two representations of money.

    Cause: a SerializerMethodField returning a raw Decimal bypasses
    DecimalField, so DRF's JSON encoder falls back to float(obj) and
    COERCE_DECIMAL_TO_STRING never applies. Asserting on response.data
    hides it, because that holds the pre-render Decimal - so this test
    renders to JSON and inspects the actual bytes.
    """
    import json

    from rest_framework.renderers import JSONRenderer

    _, client = manager_client
    response = client.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "20.00", "unit_cost": "105.00"}]},
        format="json",
    )
    assert response.status_code == 201, response.data

    on_the_wire = json.loads(JSONRenderer().render(response.data))
    assert on_the_wire["estimated_total"] == "2100.00"
    assert isinstance(on_the_wire["estimated_total"], str), (
        f"money must not be a float on the wire, got {type(on_the_wire['estimated_total']).__name__}"
    )
    # The sibling quantity it has to agree with.
    assert isinstance(on_the_wire["lines"][0]["quantity_ordered"], str)


def test_over_delivery_409_names_the_exact_row_and_amount(admin_client, manager_client, ingredient):
    """2026-09-22. The 409 used to carry only a sentence, so a client
    could tell THAT something over-delivered but not WHICH row or by how
    much without parsing English out of detail."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "10.00"}]}, format="json",
    )
    po_id = create.data["id"]
    item_id = create.data["lines"][0]["id"]
    admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/approve/",
        {"items": [{"item_id": item_id, "approved_quantity": "8.00"}]}, format="json",
    )
    admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": item_id, "received_quantity": "5.00"}]}, format="json",
    )

    response = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [{"item_id": item_id, "received_quantity": "6.00"}]}, format="json",
    )

    assert response.status_code == 409
    assert response.data["requires_confirmation"] is True
    rows = response.data["over_delivered"]
    assert len(rows) == 1
    row = rows[0]
    assert row["item_id"] == item_id
    assert row["line_id"] == item_id
    assert row["ingredient_name"] == ingredient.name
    assert row["already_received"] == "5.00"
    assert row["attempted"] == "11.00"   # 5 already + 6 now
    assert row["approved"] == "8.00"
    assert row["excess"] == "3.00"       # 11 - 8

    ingredient.refresh_from_db()
    assert ingredient.current_stock == Decimal("15.00"), "the refused receipt must not move stock"


def test_over_delivery_reports_every_offending_line(admin_client, manager_client, ingredient, restaurant):
    """Multiple bad lines each get their own entry, not one merged blob."""
    from apps.inventory.models import Ingredient

    _, admin_c = admin_client
    _, manager_c = manager_client
    second = Ingredient.objects.create(
        restaurant=restaurant, name="Second Item", unit="L",
        current_stock=Decimal("0.00"), minimum_stock_level=Decimal("1.00"),
    )

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [
            {"ingredient": str(ingredient.id), "quantity_ordered": "5.00"},
            {"ingredient": str(second.id), "quantity_ordered": "5.00"},
        ]}, format="json",
    )
    po_id = create.data["id"]
    a, b = [l["id"] for l in create.data["lines"]]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")

    response = admin_c.post(
        f"/v1/inventory/purchase-orders/{po_id}/goods-receipts/",
        {"items": [
            {"item_id": a, "received_quantity": "9.00"},
            {"item_id": b, "received_quantity": "7.00"},
        ]}, format="json",
    )

    assert response.status_code == 409
    rows = {r["item_id"]: r for r in response.data["over_delivered"]}
    assert set(rows) == {a, b}
    assert rows[a]["excess"] == "4.00"   # 9 vs 5
    assert rows[b]["excess"] == "2.00"   # 7 vs 5


def test_discrepancy_carries_item_id_matching_the_request_key(admin_client, manager_client, ingredient):
    """The response said line_id while requests take item_id. Both are
    present now, same value, so neither side has to remember which
    direction it is going."""
    _, admin_c = admin_client
    _, manager_c = manager_client

    create = manager_c.post(
        "/v1/inventory/purchase-orders/",
        {"lines": [{"ingredient": str(ingredient.id), "quantity_ordered": "10.00"}]}, format="json",
    )
    po_id = create.data["id"]
    item_id = create.data["lines"][0]["id"]
    admin_c.post(f"/v1/inventory/purchase-orders/{po_id}/approve/", {}, format="json")

    report = admin_c.get(f"/v1/inventory/purchase-orders/{po_id}/discrepancy/")

    assert report.status_code == 200
    row = report.data["lines"][0]
    assert row["item_id"] == item_id
    assert row["line_id"] == item_id


def test_wastage_summary_returns_decimal_strings_and_local_timestamps(manager_client, ingredient):
    """2026-09-22. This view hand-builds a plain dict instead of going
    through a serializer, and that quietly changed the wire format twice.

    A raw Decimal never reaches DecimalField, so DRF's encoder fell back
    to float() - total_cost came out as 55.0 while every
    serializer-backed endpoint returns "55.00". A raw datetime likewise
    skips DateTimeField, whose enforce_timezone() is what applies
    TIME_ZONE, so recorded_at came out in UTC with a Z while every other
    timestamp in this API carries +05:30. Two formats in one API,
    decided by an implementation detail no client can see.

    Renders to JSON deliberately - response.data still holds the raw
    Decimal, so asserting there cannot catch either problem.
    """
    import json

    from rest_framework.renderers import JSONRenderer

    _, client = manager_client
    client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/add-stock/",
        {"quantity": "20.00", "unit_cost": "10.00", "adjustment_reason": "OPENING_STOCK"}, format="json",
    )
    client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
        {"quantity": "1.50", "wastage_reason": "OTHER", "reason": "probe"}, format="json",
    )

    wire = json.loads(JSONRenderer().render(client.get("/v1/inventory/wastage/").data))

    assert isinstance(wire["total_cost"], str), f"money must not be a float, got {wire['total_cost']!r}"
    assert wire["total_cost"] == "15.00"

    breakdown = wire["breakdown_by_reason"]
    # OTHER has always been accepted; this pins that it is always present
    # in the breakdown too, alongside the other three.
    assert set(breakdown) == {"SPOILED", "OVER_PREPPED", "RETURNED", "OTHER"}
    assert breakdown["OTHER"] == "15.00"
    assert breakdown["SPOILED"] == "0.00", "an unused reason reads 0.00, not absent and not 0"
    assert all(isinstance(v, str) for v in breakdown.values())

    entry = wire["entries"][0]
    assert entry["quantity"] == "1.50"
    assert entry["cost"] == "15.00"
    assert isinstance(entry["quantity"], str) and isinstance(entry["cost"], str)
    # Local offset, matching every serializer-backed timestamp.
    assert "+05:30" in entry["recorded_at"], entry["recorded_at"]
    assert not entry["recorded_at"].endswith("Z")


def test_every_wastage_reason_including_other_is_accepted(manager_client, ingredient):
    _, client = manager_client
    client.patch(
        f"/v1/inventory/ingredients/{ingredient.id}/add-stock/",
        {"quantity": "50.00", "unit_cost": "10.00", "adjustment_reason": "OPENING_STOCK"}, format="json",
    )
    for reason in ("SPOILED", "OVER_PREPPED", "RETURNED", "OTHER"):
        response = client.patch(
            f"/v1/inventory/ingredients/{ingredient.id}/record-wastage/",
            {"quantity": "1.00", "wastage_reason": reason}, format="json",
        )
        assert response.status_code == 200, (reason, response.data)
