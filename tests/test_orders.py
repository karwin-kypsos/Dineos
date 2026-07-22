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


def test_placing_order_does_not_affect_untracked_items(api_client, table):
    from apps.menu.models import Category, MenuItem

    category = Category.objects.create(name="Drinks")
    drink = MenuItem.objects.create(category=category, name="Coke", price=60)
    session, _ = table_services.get_or_create_active_session(table.id)

    response = api_client.post(
        "/v1/orders/",
        {"session_id": str(session.id), "items": [{"menu_item": drink.id, "quantity": 2}]},
        format="json",
    )
    assert response.status_code == 201
    assert not drink.prepared_portions.exists()
