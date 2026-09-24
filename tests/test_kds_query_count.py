"""The KDS board must not issue one query per order item.

2026-09-24. OrderItemSerializer reads menu_item.name, but every view
serializing orders prefetched only "items", so each item cost an extra
query. Driving the real KDS UI at dineos-kitchen.onrender.com, pairing
the device timed out at its own 30s limit, and /v1/orders/active/ was
measured swinging between 1.8s and 23.9s with 88 orders on the board.
"""
import pytest
from django.test.utils import CaptureQueriesContext
from django.db import connection

from apps.orders import services as order_services
from apps.tables import services as table_services
from apps.tables.models import Table

pytestmark = pytest.mark.django_db


def _board(restaurant, menu_item, other_item, count, prefix="Q"):
    for index in range(count):
        table = Table.objects.create(
            restaurant=restaurant, table_number=prefix + str(index), capacity=2
        )
        session, _ = table_services.get_or_create_active_session(table.id)
        order_services.place_order(session.id, [
            {"menu_item_id": menu_item.id, "quantity": 1},
            {"menu_item_id": other_item.id, "quantity": 2},
        ])


@pytest.fixture
def other_item(restaurant):
    from decimal import Decimal

    from apps.menu.models import Category, MenuItem

    category = Category.objects.create(restaurant=restaurant, name="Sides", sort_order=9)
    return MenuItem.objects.create(category=category, name="Fries", price=Decimal("90.00"))


def test_active_orders_query_count_does_not_grow_with_the_board(
    server_client, restaurant, menu_item, other_item
):
    _, client = server_client

    _board(restaurant, menu_item, other_item, 3)
    with CaptureQueriesContext(connection) as small:
        assert client.get("/v1/orders/active/").status_code == 200

    _board(restaurant, menu_item, other_item, 12, prefix="R")
    with CaptureQueriesContext(connection) as large:
        response = client.get("/v1/orders/active/")
        assert response.status_code == 200

    assert len(response.data["orders"]) == 15
    # Five times the orders, ten times the items - the query count must be
    # flat. Allowing a couple of queries of slack for auth/tenant lookups.
    assert len(large) <= len(small) + 2, (
        "query count grew with the board: %d -> %d (N+1 is back)"
        % (len(small), len(large))
    )


def test_closed_sessions_leave_the_kds_board(server_client, restaurant, menu_item, cashier_client):
    """2026-09-24. close_session never touched order status, on either the
    payment close or the manager force-close, so an order still NEW when
    its table was cleared stayed 'active' forever. Measured live: 17
    tickets on the board for tables that were already free, oldest three
    days old.
    """
    from apps.billing import services as billing_services
    from apps.tables import services as table_services

    cashier_user, _ = cashier_client
    _, client = server_client

    paid_table = Table.objects.create(restaurant=restaurant, table_number="G1", capacity=2)
    cleared_table = Table.objects.create(restaurant=restaurant, table_number="G2", capacity=2)
    live_table = Table.objects.create(restaurant=restaurant, table_number="G3", capacity=2)

    for table in (paid_table, cleared_table, live_table):
        session, _ = table_services.get_or_create_active_session(table.id)
        order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    assert len(client.get("/v1/orders/active/").data["orders"]) == 3

    # one paid, one force-closed by a manager, one still running
    paid_session = paid_table.sessions.get()
    billing_services.pay_bill(paid_session.id, "CASH", cashier_user)
    cleared_session = cleared_table.sessions.get()
    table_services.close_session(cleared_session, reason="ABANDONED", closed_by=cashier_user)

    board = client.get("/v1/orders/active/").data
    assert len(board["orders"]) == 1, [o["table_number"] for o in board["orders"]]
    assert board["orders"][0]["table_number"] == "Table G3"
    assert board["summary"]["total_orders_count"] == 1
