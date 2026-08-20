import pytest

from apps.orders import services as order_services
from apps.orders.models import Order
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def _place_order_with_two_items(session_id, menu_item, other_menu_item):
    return order_services.place_order(
        session_id,
        [
            {"menu_item_id": menu_item.id, "quantity": 1},
            {"menu_item_id": other_menu_item.id, "quantity": 1},
        ],
    )


@pytest.fixture
def cashier_with_branch(cashier_client, branch):
    # Local fixture mirroring tests/test_takeaway.py's — cashier_client alone
    # has no branch, and takeaway orders require one (see
    # apps.orders.views.TakeawayOrderView.post).
    user, client = cashier_client
    user.branch = branch
    user.save(update_fields=["branch"])
    return user, client


@pytest.fixture
def other_menu_item(restaurant):
    from decimal import Decimal

    from apps.menu.models import Category, MenuItem

    category = Category.objects.create(restaurant=restaurant, name="Beverages", sort_order=2)
    return MenuItem.objects.create(category=category, name="Masala Chai", price=Decimal("40.00"))


def test_updating_one_item_status_does_not_affect_sibling(kds_client, table, menu_item, other_menu_item):
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = _place_order_with_two_items(session.id, menu_item, other_menu_item)
    item_a, item_b = order.items.all()

    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_a.id}/status/", {"status": "accepted"}, format="json"
    )
    assert response.status_code == 200, response.data

    item_a.refresh_from_db()
    item_b.refresh_from_db()
    assert item_a.status == "ACCEPTED"
    assert item_b.status == "NEW"

    # Whole order status is untouched by a single item's progress.
    order.refresh_from_db()
    assert order.status == "NEW"


def test_item_invalid_transition_rejected(kds_client, table, menu_item, other_menu_item):
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = _place_order_with_two_items(session.id, menu_item, other_menu_item)
    item_a = order.items.first()

    # NEW -> READY directly is illegal; only NEW -> ACCEPTED is.
    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_a.id}/status/", {"status": "ready"}, format="json"
    )
    assert response.status_code == 400

    item_a.refresh_from_db()
    assert item_a.status == "NEW"


def test_item_tenant_scoped_404s_for_other_restaurants_order(kds_client, table, menu_item):
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    item = order.items.first()

    from apps.restaurant.models import Restaurant
    from apps.kitchen.models import KDSDevice

    foreign_restaurant = Restaurant.objects.create(name="Foreign Kitchen", slug="foreign-kitchen-item")
    foreign_device = KDSDevice.objects.create(restaurant=foreign_restaurant, label="Foreign KDS")

    from rest_framework.test import APIClient

    foreign_client = APIClient()
    foreign_client.credentials(HTTP_X_KDS_API_KEY=foreign_device.api_key)

    response = foreign_client.patch(
        f"/v1/orders/{order.id}/items/{item.id}/status/", {"status": "accepted"}, format="json"
    )
    assert response.status_code == 404


def test_item_status_works_for_takeaway_order(cashier_with_branch, kds_client, menu_item, other_menu_item):
    _, cashier = cashier_with_branch
    _, kitchen = kds_client

    create_resp = cashier.post(
        "/v1/orders/takeaway/",
        {
            "customer_name": "Ravi",
            "items": [
                {"menu_item": menu_item.id, "quantity": 1},
                {"menu_item": other_menu_item.id, "quantity": 1},
            ],
        },
        format="json",
    )
    assert create_resp.status_code == 201, create_resp.data
    order = Order.objects.get(id=create_resp.data["id"])
    item_a, item_b = order.items.all()

    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_a.id}/status/", {"status": "accepted"}, format="json"
    )
    assert response.status_code == 200, response.data

    item_a.refresh_from_db()
    item_b.refresh_from_db()
    assert item_a.status == "ACCEPTED"
    assert item_b.status == "NEW"


def test_order_auto_advances_to_ready_once_all_items_ready(kds_client, table, menu_item, other_menu_item):
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = _place_order_with_two_items(session.id, menu_item, other_menu_item)
    item_a, item_b = order.items.all()

    for item in (item_a, item_b):
        for target in ("accepted", "preparing"):
            response = kitchen.patch(
                f"/v1/orders/{order.id}/items/{item.id}/status/", {"status": target}, format="json"
            )
            assert response.status_code == 200, response.data

    # Bring item_a to READY — order should NOT advance yet, item_b isn't ready.
    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_a.id}/status/", {"status": "ready"}, format="json"
    )
    assert response.status_code == 200, response.data
    order.refresh_from_db()
    assert order.status == "NEW"

    # Bring item_b to READY too — now every item is READY, order auto-advances.
    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_b.id}/status/", {"status": "ready"}, format="json"
    )
    assert response.status_code == 200, response.data
    order.refresh_from_db()
    assert order.status == "READY"
    assert order.ready_at is not None


def test_order_level_status_endpoint_still_works_unchanged(kds_client, table, menu_item):
    """Regression guard: restaurants that never touch per-item status must
    keep working exactly as before through the whole-order endpoint."""
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    response = kitchen.patch(f"/v1/orders/{order.id}/status/", {"status": "accepted"}, format="json")
    assert response.status_code == 200, response.data
    order.refresh_from_db()
    assert order.status == "ACCEPTED"

    response = kitchen.patch(f"/v1/orders/{order.id}/status/", {"status": "preparing"}, format="json")
    assert response.status_code == 200
    order.refresh_from_db()
    assert order.status == "PREPARING"

    response = kitchen.patch(f"/v1/orders/{order.id}/status/", {"status": "ready"}, format="json")
    assert response.status_code == 200
    order.refresh_from_db()
    assert order.status == "READY"
    # The lone item's own status is left untouched by the whole-order path —
    # items only move via the per-item endpoint.
    assert order.items.first().status == "NEW"
