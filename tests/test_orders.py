import pytest

from apps.orders import services as order_services
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def test_insufficient_portions_returns_400(api_client, table, menu_item):
    session, _ = table_services.get_or_create_active_session(table.id)

    response = api_client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": menu_item.id, "quantity": 999}]},
        format="json",
    )
    assert response.status_code == 400


def test_kitchen_cannot_skip_status(kds_client, table, menu_item):
    _, kitchen_client = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    # NEW -> READY directly should be rejected; only NEW -> ACCEPTED is legal.
    response = kitchen_client.patch(f"/v1/orders/{order.id}/status/", {"status": "ready"}, format="json")
    assert response.status_code == 400

    order.refresh_from_db()
    assert order.status == "NEW"


def test_server_cannot_collect_before_ready(server_client, table, menu_item):
    _, server = server_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = server.patch(f"/v1/orders/{order.id}/collected/")
    assert response.status_code == 400


def test_placing_order_with_menu_item_from_another_restaurant_returns_400(api_client, table, restaurant):
    from apps.menu.models import Category, MenuItem
    from apps.restaurant.models import Restaurant

    foreign_restaurant = Restaurant.objects.create(name="Foreign Menu Co", slug="foreign-menu-co")
    foreign_category = Category.objects.create(restaurant=foreign_restaurant, name="Foreign Category")
    foreign_item = MenuItem.objects.create(category=foreign_category, name="Foreign Dish", price=99)
    session, _ = table_services.get_or_create_active_session(table.id)

    response = api_client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": foreign_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 400, response.data


def test_placing_order_with_unavailable_menu_item_returns_400(api_client, table, menu_item):
    menu_item.is_available = False
    menu_item.save(update_fields=["is_available"])
    session, _ = table_services.get_or_create_active_session(table.id)

    response = api_client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": menu_item.id, "quantity": 1}]},
        format="json",
    )

    assert response.status_code == 400, response.data


def test_my_orders_scopes_to_own_assigned_tables_across_all_active_statuses(restaurant, branch, menu_item):
    from rest_framework.test import APIClient

    from apps.authentication.models import User
    from apps.authentication.serializers import DineOSTokenObtainPairSerializer
    from apps.tables.models import Table

    server_a = User.objects.create_user(
        email="my-orders-a@demo-bistro.demo", password="Test@1234", role="SERVER",
        name="Server A", restaurant=restaurant, branch=branch,
    )
    server_b = User.objects.create_user(
        email="my-orders-b@demo-bistro.demo", password="Test@1234", role="SERVER",
        name="Server B", restaurant=restaurant, branch=branch,
    )
    table_a = Table.objects.create(restaurant=restaurant, branch=branch, table_number="MA1", capacity=4)
    table_b = Table.objects.create(restaurant=restaurant, branch=branch, table_number="MB1", capacity=4)

    session_a, _ = table_services.get_or_create_active_session(table_a.id)
    order_a = order_services.place_order(session_a.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    session_a.refresh_from_db()
    session_a.assigned_server = server_a
    session_a.save(update_fields=["assigned_server"])
    order_services.advance_kitchen_status(order_a.id, "ACCEPTED")

    session_b, _ = table_services.get_or_create_active_session(table_b.id)
    order_b = order_services.place_order(session_b.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    session_b.refresh_from_db()
    session_b.assigned_server = server_b
    session_b.save(update_fields=["assigned_server"])

    token = DineOSTokenObtainPairSerializer.get_token(server_a)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

    response = client.get("/v1/orders/mine/")

    assert response.status_code == 200
    order_ids = {o["id"] for o in response.data}
    assert str(order_a.id) in order_ids
    assert str(order_b.id) not in order_ids
    returned_order_a = next(o for o in response.data if o["id"] == str(order_a.id))
    assert returned_order_a["status"] == "ACCEPTED"


def test_order_response_includes_raw_table_number(api_client, table, menu_item):
    """Regression (2026-08-23, Shereena): GET /v1/orders/mine/ (and every
    other order-list endpoint, all served by OrderSerializer) only had the
    table's raw id, not its human-readable table_number."""
    session, _ = table_services.get_or_create_active_session(table.id)
    order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = api_client.get(f"/v1/orders/session/{session.id}/")

    assert response.status_code == 200
    assert response.data[0]["table_number"] == table.table_number


def test_takeaway_order_response_has_null_table_number(cashier_client, menu_item, branch):
    cashier_user, client = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])

    response = client.post(
        "/v1/orders/takeaway/", {"items": [{"menu_item": menu_item.id, "quantity": 1}]}, format="json",
    )

    assert response.status_code == 201, response.data
    assert response.data["table"] is None
    assert response.data["table_number"] is None


def test_placing_order_does_not_affect_untracked_items(api_client, table):
    from apps.menu.models import Category, MenuItem

    category = Category.objects.create(restaurant=table.restaurant, name="Drinks")
    drink = MenuItem.objects.create(category=category, name="Coke", price=60)
    session, _ = table_services.get_or_create_active_session(table.id)

    response = api_client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": drink.id, "quantity": 2}]},
        format="json",
    )
    assert response.status_code == 201
    assert not drink.prepared_portions.exists()
