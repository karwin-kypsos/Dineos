"""GET /v1/orders/service-status/ — per-table served/unserved rollup.

2026-09-21, per Karwin: "there is no clear place to track whether an order
has been served for a specific table".
"""
import pytest

from apps.orders import services as order_services
from apps.orders.models import Order
from apps.tables import services as table_services
from apps.tables.models import Table

pytestmark = pytest.mark.django_db


def _make_server(restaurant, branch, email, name):
    from apps.authentication.models import User

    return User.objects.create_user(
        email=email, password="Test@1234", role="SERVER", name=name,
        restaurant=restaurant, branch=branch,
    )


def _client_for(user):
    from rest_framework.test import APIClient

    from apps.authentication.serializers import DineOSTokenObtainPairSerializer

    token = DineOSTokenObtainPairSerializer.get_token(user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return client


def _seat(restaurant, branch, number, server, menu_item, rounds=1):
    table = Table.objects.create(restaurant=restaurant, branch=branch, table_number=number, capacity=4)
    session, _ = table_services.get_or_create_active_session(table.id)
    session.assigned_server = server
    session.save(update_fields=["assigned_server"])
    orders = []
    for _ in range(rounds):
        orders.append(order_services.place_order(
            session.id, [{"menu_item_id": menu_item.id, "quantity": 1}], placed_by=server,
        ))
    return table, session, orders


def _advance_to_served(order):
    for step in ("ACCEPTED", "PREPARING", "READY"):
        order_services.advance_kitchen_status(order.id, step)
    order_services.mark_collected(order.id)
    order_services.mark_served(order.id)


def test_rollup_counts_served_and_unserved_per_table(restaurant, branch, menu_item):
    server = _make_server(restaurant, branch, "svc-a@demo.test", "Svc A")
    client = _client_for(server)

    table, _, orders = _seat(restaurant, branch, "S1", server, menu_item, rounds=3)
    _advance_to_served(orders[0])

    response = client.get("/v1/orders/service-status/")
    assert response.status_code == 200, response.data

    row = [r for r in response.data if r["table_id"] == str(table.id)][0]
    assert row["table_number"] == "S1"
    assert row["total_orders"] == 3
    assert row["served_count"] == 1
    assert row["unserved_count"] == 2
    assert row["all_served"] is False
    assert row["assigned_server_id"] == str(server.id)

    served = [o for o in row["orders"] if o["is_served"]]
    assert len(served) == 1
    assert served[0]["status"] == "SERVED"
    assert served[0]["served_at"] is not None
    assert served[0]["items"] == [{"menu_item_name": menu_item.name, "quantity": 1}]
    assert all(o["served_at"] is None for o in row["orders"] if not o["is_served"])


def test_all_served_flips_once_every_order_is_served(restaurant, branch, menu_item):
    server = _make_server(restaurant, branch, "svc-b@demo.test", "Svc B")
    client = _client_for(server)
    table, _, orders = _seat(restaurant, branch, "S2", server, menu_item, rounds=2)

    _advance_to_served(orders[0])
    row = [r for r in client.get("/v1/orders/service-status/").data if r["table_id"] == str(table.id)][0]
    assert row["all_served"] is False

    _advance_to_served(orders[1])
    row = [r for r in client.get("/v1/orders/service-status/").data if r["table_id"] == str(table.id)][0]
    assert row["all_served"] is True
    assert row["unserved_count"] == 0


def test_server_sees_only_their_own_tables(restaurant, branch, menu_item):
    server_a = _make_server(restaurant, branch, "svc-c@demo.test", "Svc C")
    server_b = _make_server(restaurant, branch, "svc-d@demo.test", "Svc D")

    table_a, _, _ = _seat(restaurant, branch, "S3", server_a, menu_item)
    table_b, _, _ = _seat(restaurant, branch, "S4", server_b, menu_item)

    ids = {r["table_id"] for r in _client_for(server_a).get("/v1/orders/service-status/").data}
    assert str(table_a.id) in ids
    assert str(table_b.id) not in ids


def test_manager_sees_the_whole_branch_not_just_one_server(restaurant, branch, menu_item, manager_client):
    user, client = manager_client
    user.branch = branch
    user.save(update_fields=["branch"])

    server_a = _make_server(restaurant, branch, "svc-e@demo.test", "Svc E")
    server_b = _make_server(restaurant, branch, "svc-f@demo.test", "Svc F")
    table_a, _, _ = _seat(restaurant, branch, "S5", server_a, menu_item)
    table_b, _, _ = _seat(restaurant, branch, "S6", server_b, menu_item)

    ids = {r["table_id"] for r in client.get("/v1/orders/service-status/").data}
    assert {str(table_a.id), str(table_b.id)} <= ids


def test_table_query_param_narrows_to_one_table(restaurant, branch, menu_item):
    server = _make_server(restaurant, branch, "svc-g@demo.test", "Svc G")
    client = _client_for(server)
    table_a, _, _ = _seat(restaurant, branch, "S7", server, menu_item)
    _seat(restaurant, branch, "S8", server, menu_item)

    data = client.get(f"/v1/orders/service-status/?table={table_a.id}").data
    assert [r["table_id"] for r in data] == [str(table_a.id)]

    # Malformed id returns empty rather than raising, same convention as
    # the other filters in this module.
    assert client.get("/v1/orders/service-status/?table=not-a-uuid").data == []


def test_cancelled_orders_do_not_hold_a_table_open_forever(restaurant, branch, menu_item):
    server = _make_server(restaurant, branch, "svc-h@demo.test", "Svc H")
    client = _client_for(server)
    table, _, orders = _seat(restaurant, branch, "S9", server, menu_item, rounds=2)

    _advance_to_served(orders[0])
    cancelled = orders[1]
    cancelled.status = Order.Status.CANCELLED
    cancelled.save(update_fields=["status"])

    row = [r for r in client.get("/v1/orders/service-status/").data if r["table_id"] == str(table.id)][0]
    assert row["total_orders"] == 1
    assert row["all_served"] is True, "a cancelled order must not count as unserved work"
