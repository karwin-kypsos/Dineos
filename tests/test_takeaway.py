from decimal import Decimal

import pytest

from apps.orders.models import Order

pytestmark = pytest.mark.django_db


@pytest.fixture
def cashier_with_branch(cashier_client, branch):
    user, client = cashier_client
    user.branch = branch
    user.save(update_fields=["branch"])
    return user, client


def test_staff_without_branch_cannot_place_takeaway_order(cashier_client, menu_item):
    _, client = cashier_client

    response = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Sam", "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 403


def test_place_takeaway_order_has_no_table_or_session(cashier_with_branch, menu_item, branch):
    _, client = cashier_with_branch

    response = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Sam", "customer_phone": "9998887777",
         "items": [{"menu_item": menu_item.id, "quantity": 2}]},
        format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["order_type"] == "TAKEAWAY"
    assert response.data["table"] is None
    assert response.data["session"] is None
    assert str(response.data["branch"]) == str(branch.id)
    assert response.data["customer_name"] == "Sam"

    order = Order.objects.get(id=response.data["id"])
    assert order.table is None
    assert order.session is None


def test_takeaway_order_decrements_portions(cashier_with_branch, menu_item):
    _, client = cashier_with_branch
    initial_remaining = menu_item.prepared_portions.get().portions_remaining

    response = client.post(
        "/v1/orders/takeaway/",
        {"items": [{"menu_item": menu_item.id, "quantity": 3}]}, format="json",
    )

    assert response.status_code == 201
    menu_item.prepared_portions.get().refresh_from_db()
    assert menu_item.prepared_portions.get().portions_remaining == initial_remaining - 3


def test_takeaway_bill_preview_and_payment(cashier_with_branch, menu_item, branch):
    _, client = cashier_with_branch

    create = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 2}]}, format="json",
    )
    order_id = create.data["id"]

    preview = client.get(f"/v1/bills/takeaway/{order_id}/")
    assert preview.status_code == 200
    expected_subtotal = menu_item.price * 2
    assert Decimal(preview.data["subtotal"]) == expected_subtotal
    assert len(preview.data["items"]) == 1
    assert preview.data["items"][0]["menu_item_name"] == menu_item.name
    assert preview.data["items"][0]["quantity"] == 2
    assert preview.data["branch_name"] == branch.name

    pay = client.post(
        "/v1/bills/takeaway-payment/", {"order_id": order_id, "payment_method": "CASH"}, format="json",
    )
    assert pay.status_code == 201, pay.data
    assert str(pay.data["order"]) == order_id
    assert pay.data["session"] is None
    assert len(pay.data["items"]) == 1
    assert pay.data["table_number"] is None  # takeaway has no table

    # Idempotent replay
    pay_again = client.post(
        "/v1/bills/takeaway-payment/", {"order_id": order_id, "payment_method": "CASH"}, format="json",
    )
    assert pay_again.status_code == 201
    assert str(pay_again.data["id"]) == str(pay.data["id"])


def test_cannot_pay_takeaway_order_from_another_restaurant(cashier_with_branch, restaurant):
    _, client = cashier_with_branch

    from apps.restaurant.models import Branch, Restaurant

    foreign_restaurant = Restaurant.objects.create(name="Foreign TA", slug="foreign-takeaway")
    foreign_branch = Branch.objects.create(restaurant=foreign_restaurant, name="Foreign Branch")
    foreign_order = Order.objects.create(order_type="TAKEAWAY", branch=foreign_branch, round_number=1)

    response = client.post(
        "/v1/bills/takeaway-payment/", {"order_id": str(foreign_order.id), "payment_method": "CASH"}, format="json",
    )

    assert response.status_code == 403


def test_daily_collections_includes_takeaway_revenue(cashier_with_branch, menu_item, restaurant):
    """Regression: daily_collections used to only sum dine-in (session-based)
    bills, silently excluding takeaway payments."""
    _, client = cashier_with_branch

    create = client.post("/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json")
    order_id = create.data["id"]
    pay = client.post("/v1/bills/takeaway-payment/", {"order_id": order_id, "payment_method": "CASH"}, format="json")
    assert pay.status_code == 201

    response = client.get("/v1/cashier/collections/daily/")

    assert response.status_code == 200
    assert response.data["tables_served"] >= 1
    assert Decimal(str(response.data["total_collected"])) >= Decimal(str(pay.data["total_amount"]))


def test_order_response_includes_total_amount(cashier_with_branch, menu_item):
    _, client = cashier_with_branch

    response = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 2}]}, format="json",
    )

    assert response.status_code == 201
    assert Decimal(str(response.data["total_amount"])) == menu_item.price * 2


def test_list_takeaway_orders_scoped_to_own_branch(cashier_with_branch, menu_item, restaurant):
    _, client = cashier_with_branch

    from apps.restaurant.models import Branch

    client.post("/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json")

    other_branch = Branch.objects.create(restaurant=restaurant, name="Other Branch")
    Order.objects.create(order_type="TAKEAWAY", branch=other_branch, round_number=1)

    response = client.get("/v1/orders/takeaway/")

    assert response.status_code == 200
    assert len(response.data) == 1
    assert response.data[0]["branch"] is not None


def test_list_takeaway_orders_filters_by_status(cashier_with_branch, menu_item):
    _, client = cashier_with_branch

    new_order = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )
    order_id = new_order.data["id"]
    Order.objects.filter(id=order_id).update(status="READY")

    ready_response = client.get("/v1/orders/takeaway/?status=ready")
    assert ready_response.status_code == 200
    assert len(ready_response.data) == 1
    assert ready_response.data[0]["status"] == "READY"

    preparing_response = client.get("/v1/orders/takeaway/?status=preparing")
    assert preparing_response.status_code == 200
    assert preparing_response.data == []

    all_response = client.get("/v1/orders/takeaway/?status=all")
    assert all_response.status_code == 200
    assert len(all_response.data) == 1


def test_active_orders_includes_takeaway(cashier_with_branch, manager_client, menu_item):
    # Active/Ready Orders are Server/Manager/KDS screens (IsServerOrKDSDevice
    # excludes Cashier) — a Cashier creates the takeaway order, a Manager
    # confirms it shows up alongside dine-in orders.
    _, cashier = cashier_with_branch
    _, manager = manager_client

    create = cashier.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )
    order_id = create.data["id"]
    Order.objects.filter(id=order_id).update(status="PREPARING")

    response = manager.get("/v1/orders/active/")

    assert response.status_code == 200
    ids = {o["id"] for o in response.data}
    assert order_id in ids


def test_ready_orders_includes_takeaway(cashier_with_branch, manager_client, menu_item):
    _, cashier = cashier_with_branch
    _, manager = manager_client

    create = cashier.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )
    order_id = create.data["id"]
    Order.objects.filter(id=order_id).update(status="READY")

    response = manager.get("/v1/orders/ready/")

    assert response.status_code == 200
    ids = {o["id"] for o in response.data}
    assert order_id in ids


def test_kitchen_disabled_takeaway_order_auto_serves(cashier_with_branch, menu_item, restaurant):
    restaurant.kitchen_enabled = False
    restaurant.save(update_fields=["kitchen_enabled"])
    _, client = cashier_with_branch

    response = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )

    assert response.status_code == 201
    assert response.data["status"] == "SERVED"
