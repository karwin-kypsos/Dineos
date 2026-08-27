from decimal import Decimal

import pytest
from django.utils import timezone

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


def test_kitchen_can_fetch_takeaway_order_detail(cashier_with_branch, kds_client, menu_item):
    _, cashier = cashier_with_branch
    _, kitchen = kds_client

    create_resp = cashier.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Priya", "customer_phone": "9998887766",
         "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    assert create_resp.status_code == 201, create_resp.data

    response = kitchen.get(f"/v1/orders/{create_resp.data['id']}/")

    assert response.status_code == 200, response.data
    assert response.data["order_type"] == "TAKEAWAY"
    assert response.data["table"] is None


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


def test_list_takeaway_orders_defaults_to_today_not_all_time(cashier_with_branch, menu_item):
    """New: date scoping (2026-08-27, per Shereena's bug report — this
    endpoint returned every takeaway order ever placed, not just the ones
    relevant to today/the cashier's current shift)."""
    from datetime import timedelta

    _, client = cashier_with_branch

    old_order = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    ).data
    Order.objects.filter(id=old_order["id"]).update(placed_at=timezone.now() - timedelta(days=3))

    today_order = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    ).data

    default_response = client.get("/v1/orders/takeaway/?status=all")
    ids = {o["id"] for o in default_response.data}
    assert today_order["id"] in ids
    assert old_order["id"] not in ids

    today = timezone.localdate()
    ranged_response = client.get(
        f"/v1/orders/takeaway/?status=all&date_from={(today - timedelta(days=5)).isoformat()}&date_to={today.isoformat()}"
    )
    ranged_ids = {o["id"] for o in ranged_response.data}
    assert old_order["id"] in ranged_ids
    assert today_order["id"] in ranged_ids


def test_list_takeaway_orders_excludes_collected_and_served_by_default(cashier_with_branch, menu_item):
    """New: once collected/served, a takeaway order drops out of the
    default active queue (2026-08-27, per Shereena) — ?status=all still
    shows it explicitly."""
    _, client = cashier_with_branch

    active_order = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    ).data
    served_order = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    ).data
    Order.objects.filter(id=served_order["id"]).update(status="SERVED")

    default_response = client.get("/v1/orders/takeaway/")
    default_ids = {o["id"] for o in default_response.data}
    assert active_order["id"] in default_ids
    assert served_order["id"] not in default_ids

    all_response = client.get("/v1/orders/takeaway/?status=all")
    all_ids = {o["id"] for o in all_response.data}
    assert active_order["id"] in all_ids
    assert served_order["id"] in all_ids


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
    # /v1/orders/active/ returns a Dashboard Envelope ({server_time, summary,
    # orders}), not a flat array — see tests/test_active_orders_envelope.py
    # for full coverage of the envelope shape itself.
    ids = {o["id"] for o in response.data["orders"]}
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


def test_adding_a_round_to_a_takeaway_order_links_it_and_increments_round_number(cashier_with_branch, menu_item):
    _, client = cashier_with_branch

    first = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Sam", "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    assert first.status_code == 201, first.data
    root_id = first.data["id"]
    assert first.data["round_number"] == 1
    assert first.data["parent_order"] is None

    second = client.post(
        "/v1/orders/takeaway/",
        {"existing_order_id": root_id, "items": [{"menu_item": menu_item.id, "quantity": 2}]},
        format="json",
    )
    assert second.status_code == 201, second.data
    assert second.data["round_number"] == 2
    assert str(second.data["parent_order"]) == root_id
    # customer info carries forward from the root order automatically
    assert second.data["customer_name"] == "Sam"
    assert second.data["id"] != root_id

    third = client.post(
        "/v1/orders/takeaway/",
        {"existing_order_id": second.data["id"], "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    assert third.status_code == 201, third.data
    assert third.data["round_number"] == 3
    # passing a later round's id still resolves back to the same root
    assert str(third.data["parent_order"]) == root_id


def test_cannot_add_takeaway_round_to_another_restaurants_order(cashier_with_branch, menu_item, restaurant):
    _, client = cashier_with_branch

    from apps.restaurant.models import Branch, Restaurant

    foreign_restaurant = Restaurant.objects.create(name="Foreign TA Rounds", slug="foreign-ta-rounds")
    foreign_branch = Branch.objects.create(restaurant=foreign_restaurant, name="Foreign Branch")
    foreign_order = Order.objects.create(order_type="TAKEAWAY", branch=foreign_branch, round_number=1)

    response = client.post(
        "/v1/orders/takeaway/",
        {"existing_order_id": str(foreign_order.id), "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 403


def test_cannot_add_takeaway_round_once_order_is_billed(cashier_with_branch, menu_item):
    _, client = cashier_with_branch

    first = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )
    order_id = first.data["id"]
    pay = client.post(
        "/v1/bills/takeaway-payment/", {"order_id": order_id, "payment_method": "CASH"}, format="json",
    )
    assert pay.status_code == 201

    response = client.post(
        "/v1/orders/takeaway/",
        {"existing_order_id": order_id, "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 409


def test_takeaway_bill_preview_and_payment_combine_every_round(cashier_with_branch, menu_item):
    _, client = cashier_with_branch

    first = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 2}]}, format="json",
    )
    root_id = first.data["id"]
    second = client.post(
        "/v1/orders/takeaway/",
        {"existing_order_id": root_id, "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    assert second.status_code == 201, second.data

    preview = client.get(f"/v1/bills/takeaway/{root_id}/")
    assert preview.status_code == 200
    assert Decimal(preview.data["subtotal"]) == menu_item.price * 3
    # One line item per round (each round is its own OrderItem row, even
    # for the same menu item) — this stays consistent with how a multi-round
    # dine-in bill already lists items, see services.line_items.
    assert len(preview.data["items"]) == 2
    assert sorted(item["quantity"] for item in preview.data["items"]) == [1, 2]

    # Previewing/paying via the SECOND round's id resolves to the same bill.
    preview_via_round = client.get(f"/v1/bills/takeaway/{second.data['id']}/")
    assert Decimal(preview_via_round.data["subtotal"]) == menu_item.price * 3

    pay = client.post(
        "/v1/bills/takeaway-payment/", {"order_id": second.data["id"], "payment_method": "CASH"}, format="json",
    )
    assert pay.status_code == 201, pay.data
    assert str(pay.data["order"]) == root_id
    assert Decimal(str(pay.data["total_amount"])) == Decimal(str(preview.data["total_amount"]))


def test_takeaway_order_details_returns_every_round(cashier_with_branch, menu_item):
    _, client = cashier_with_branch

    first = client.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Priya", "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    root_id = first.data["id"]
    client.post(
        "/v1/orders/takeaway/",
        {"existing_order_id": root_id, "items": [{"menu_item": menu_item.id, "quantity": 2}]},
        format="json",
    )

    response = client.get(f"/v1/orders/takeaway/{root_id}/details/")

    assert response.status_code == 200, response.data
    assert response.data["order_id"] == root_id
    assert response.data["customer_name"] == "Priya"
    assert response.data["is_billed"] is False
    assert len(response.data["rounds"]) == 2
    assert [r["round_number"] for r in response.data["rounds"]] == [1, 2]
    assert Decimal(str(response.data["combined_total_amount"])) == menu_item.price * 3


def test_takeaway_order_payment_status_pending_then_paid(cashier_with_branch, menu_item):
    _, client = cashier_with_branch

    create = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )
    assert create.data["payment_status"] == "PENDING"
    order_id = create.data["id"]

    preview = client.get(f"/v1/bills/takeaway/{order_id}/")
    assert preview.data["payment_status"] == "PENDING"

    pay = client.post(
        "/v1/bills/takeaway-payment/", {"order_id": order_id, "payment_method": "CASH"}, format="json",
    )
    assert pay.status_code == 201
    assert pay.data["payment_status"] == "PAID"

    detail = client.get(f"/v1/orders/takeaway/{order_id}/details/")
    assert detail.data["rounds"][0]["payment_status"] == "PAID"


def test_kitchen_disabled_takeaway_order_auto_serves(cashier_with_branch, menu_item, restaurant):
    restaurant.kitchen_enabled = False
    restaurant.save(update_fields=["kitchen_enabled"])
    _, client = cashier_with_branch

    response = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )

    assert response.status_code == 201
    assert response.data["status"] == "SERVED"
