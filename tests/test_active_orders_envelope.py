"""GET /v1/orders/active/ — Dashboard Envelope shape (Shereena's KOT Live
spec, 2026-08-21). Was a flat OrderSerializer array before; now returns
{server_time, summary, orders} with each order carrying pre-calculated KDS
card metrics (elapsed wait time, urgency, item-status breakdown).

Time-dependent assertions follow this codebase's existing convention (see
tests/test_inventory.py's wastage-log test) of directly `.update()`-ing the
relevant timestamp column rather than mocking django.utils.timezone.now().
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.orders import services as order_services
from apps.orders.models import Order, OrderItem
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


@pytest.fixture
def cashier_with_branch(cashier_client, branch):
    user, client = cashier_client
    user.branch = branch
    user.save(update_fields=["branch"])
    return user, client


def _place_dine_in_order(table, menu_item, quantity=1):
    session, _ = table_services.get_or_create_active_session(table.id)
    return order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": quantity}])


def test_active_orders_envelope_shape(kds_client, table, menu_item):
    _, client = kds_client
    _place_dine_in_order(table, menu_item)

    response = client.get("/v1/orders/active/")

    assert response.status_code == 200
    assert set(response.data.keys()) == {"server_time", "summary", "orders"}
    assert set(response.data["summary"].keys()) == {
        "total_orders_count", "new_count", "accepted_count", "preparing_count", "ready_count",
    }
    assert isinstance(response.data["orders"], list)
    assert len(response.data["orders"]) == 1


def test_summary_counts_match_status_mix(kds_client, table, menu_item):
    _, client = kds_client

    _place_dine_in_order(table, menu_item)  # stays NEW
    accepted_order = _place_dine_in_order(table, menu_item)
    Order.objects.filter(id=accepted_order.id).update(status="ACCEPTED")
    preparing_order = _place_dine_in_order(table, menu_item)
    Order.objects.filter(id=preparing_order.id).update(status="PREPARING")

    response = client.get("/v1/orders/active/")
    summary = response.data["summary"]

    assert summary["total_orders_count"] == 3
    assert summary["new_count"] == 1
    assert summary["accepted_count"] == 1
    assert summary["preparing_count"] == 1
    assert summary["ready_count"] == 0  # active list never includes READY orders


def test_table_number_dine_in_vs_takeaway(cashier_with_branch, kds_client, table, menu_item):
    _, cashier = cashier_with_branch
    _, client = kds_client

    _place_dine_in_order(table, menu_item)
    takeaway_resp = cashier.post(
        "/v1/orders/takeaway/",
        {"customer_name": "Sam", "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )
    assert takeaway_resp.status_code == 201, takeaway_resp.data

    response = client.get("/v1/orders/active/")
    table_numbers = {o["table_number"] for o in response.data["orders"]}

    # table_number = "Takeaway" for takeaway orders is this session's own
    # interpretation (flagged to Shereena) — there's no established
    # convention yet for what a takeaway card's table_number should read.
    assert table_numbers == {f"Table {table.table_number}", "Takeaway"}


def test_elapsed_seconds_and_formatted(kds_client, table, menu_item):
    _, client = kds_client
    order = _place_dine_in_order(table, menu_item)
    Order.objects.filter(id=order.id).update(placed_at=timezone.now() - timedelta(seconds=320))

    response = client.get("/v1/orders/active/")
    card = response.data["orders"][0]

    assert card["elapsed_seconds"] >= 320
    minutes, seconds = divmod(card["elapsed_seconds"], 60)
    assert card["elapsed_formatted"] == f"{minutes:02d}:{seconds:02d}"
    assert card["is_urgent"] is False


def test_is_urgent_past_ten_minute_threshold(kds_client, table, menu_item):
    _, client = kds_client
    order = _place_dine_in_order(table, menu_item)
    Order.objects.filter(id=order.id).update(placed_at=timezone.now() - timedelta(seconds=601))

    response = client.get("/v1/orders/active/")
    card = response.data["orders"][0]

    assert card["elapsed_seconds"] > 600
    assert card["is_urgent"] is True


def test_item_count_breakdown(kds_client, table, menu_item):
    _, client = kds_client
    order = _place_dine_in_order(table, menu_item)  # 1 item, defaults to NEW

    OrderItem.objects.create(
        order=order, menu_item=menu_item, quantity=1, unit_price=menu_item.price, status="ACCEPTED",
    )
    OrderItem.objects.create(
        order=order, menu_item=menu_item, quantity=1, unit_price=menu_item.price, status="PREPARING",
    )
    OrderItem.objects.create(
        order=order, menu_item=menu_item, quantity=1, unit_price=menu_item.price, status="READY",
    )

    response = client.get("/v1/orders/active/")
    card = response.data["orders"][0]

    assert card["total_items_count"] == 4
    assert card["pending_items_count"] == 1  # the original NEW item
    assert card["cooking_items_count"] == 2  # ACCEPTED + PREPARING
    assert card["ready_items_count"] == 1
    assert Decimal(str(card["total_amount"])) > 0  # unaffected pass-through field, sanity check
