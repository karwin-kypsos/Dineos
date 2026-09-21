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

    # A sibling's own status is untouched by item_a's progress, but the
    # order itself auto-advances NEW -> ACCEPTED the moment the first item
    # is acknowledged (2026-08-28 — see _maybe_auto_advance_order).
    order.refresh_from_db()
    assert order.status == "ACCEPTED"


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


def test_order_level_tenant_scoped_404s_for_other_restaurants_order(kds_client, table, menu_item):
    # Whole-order counterpart to test_item_tenant_scoped_404s_for_other_restaurants_order
    # above - advance_kitchen_status() fetches the order by bare id with no
    # tenant check, so the view itself must scope the lookup first, or a KDS
    # device from a different restaurant could advance this order's status.
    session, _ = table_services.get_or_create_active_session(table.id)
    order = order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    from apps.restaurant.models import Restaurant
    from apps.kitchen.models import KDSDevice

    foreign_restaurant = Restaurant.objects.create(name="Foreign Kitchen", slug="foreign-kitchen-order")
    foreign_device = KDSDevice.objects.create(restaurant=foreign_restaurant, label="Foreign KDS")

    from rest_framework.test import APIClient

    foreign_client = APIClient()
    foreign_client.credentials(HTTP_X_KDS_API_KEY=foreign_device.api_key)

    response = foreign_client.patch(f"/v1/orders/{order.id}/status/", {"status": "accepted"}, format="json")
    assert response.status_code == 404

    order.refresh_from_db()
    assert order.status == "NEW"


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

    # By now both items have passed through accepted+preparing, so the order
    # has already auto-advanced NEW -> ACCEPTED -> PREPARING off item_a's
    # progress alone. Bring item_a to READY — order should NOT advance to
    # READY yet, item_b isn't ready.
    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_a.id}/status/", {"status": "ready"}, format="json"
    )
    assert response.status_code == 200, response.data
    order.refresh_from_db()
    assert order.status == "PREPARING"

    # Bring item_b to READY too — now every item is READY, order auto-advances.
    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_b.id}/status/", {"status": "ready"}, format="json"
    )
    assert response.status_code == 200, response.data
    order.refresh_from_db()
    assert order.status == "READY"
    assert order.ready_at is not None


def test_order_level_status_endpoint_cascades_to_all_items(kds_client, table, menu_item, other_menu_item):
    """Whole-order PATCH .../status/ must cascade every item forward to
    match, in the same transaction — restaurants that never touch the
    per-item endpoint still see item statuses stay in sync with the order."""
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = _place_order_with_two_items(session.id, menu_item, other_menu_item)

    response = kitchen.patch(f"/v1/orders/{order.id}/status/", {"status": "accepted"}, format="json")
    assert response.status_code == 200, response.data
    order.refresh_from_db()
    assert order.status == "ACCEPTED"
    assert all(item.status == "ACCEPTED" for item in order.items.all())

    response = kitchen.patch(f"/v1/orders/{order.id}/status/", {"status": "preparing"}, format="json")
    assert response.status_code == 200
    order.refresh_from_db()
    assert order.status == "PREPARING"
    assert order.preparing_at is not None
    assert all(item.status == "PREPARING" for item in order.items.all())

    response = kitchen.patch(f"/v1/orders/{order.id}/status/", {"status": "ready"}, format="json")
    assert response.status_code == 200
    order.refresh_from_db()
    assert order.status == "READY"
    assert all(item.status == "READY" for item in order.items.all())


def test_order_level_cascade_does_not_downgrade_an_item_already_ahead(kds_client, table, menu_item, other_menu_item):
    """An item advanced ahead via the per-item endpoint alone must never be
    pushed backward once the whole-order endpoint catches up to it —
    cascade only fills items in, it never overwrites one already at/past
    the target status."""
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = _place_order_with_two_items(session.id, menu_item, other_menu_item)
    item_a, item_b = order.items.all()

    # Walk item_a all the way to READY via the per-item endpoint alone,
    # without ever touching the whole-order endpoint. The order auto-follows
    # item_a's own progress as far as it can (NEW -> ACCEPTED -> PREPARING),
    # but stops at PREPARING — READY auto-advance only fires once EVERY item
    # is ready, and item_b (still NEW) hasn't moved — so item_a ends up
    # ahead of the order, which is still only at PREPARING.
    for target in ("accepted", "preparing", "ready"):
        response = kitchen.patch(f"/v1/orders/{order.id}/items/{item_a.id}/status/", {"status": target}, format="json")
        assert response.status_code == 200, response.data

    order.refresh_from_db()
    assert order.status == "PREPARING"

    # Now mark the whole order ready — item_b (still NEW) should cascade
    # straight up to READY, but item_a must stay exactly at READY, not get
    # pulled back.
    response = kitchen.patch(f"/v1/orders/{order.id}/status/", {"status": "ready"}, format="json")
    assert response.status_code == 200, response.data
    order.refresh_from_db()
    item_a.refresh_from_db()
    item_b.refresh_from_db()
    assert order.status == "READY"
    assert item_a.status == "READY"
    assert item_b.status == "READY"


def test_item_reaching_accepted_auto_advances_order_from_new(kds_client, table, menu_item, other_menu_item):
    """Regression (2026-08-28, Shereena — Server app's realtime feed never
    updated between an order being placed and it becoming Ready, since the
    order's own status stayed frozen at NEW the whole time when the kitchen
    drove items instead of the whole-order endpoint). First item to be
    acknowledged advances the ORDER to ACCEPTED, without forcing the
    still-NEW sibling to jump ahead of its own individual progress."""
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = _place_order_with_two_items(session.id, menu_item, other_menu_item)
    item_a, item_b = order.items.all()

    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_a.id}/status/", {"status": "accepted"}, format="json"
    )
    assert response.status_code == 200, response.data

    order.refresh_from_db()
    item_a.refresh_from_db()
    item_b.refresh_from_db()
    assert order.status == "ACCEPTED"
    assert order.accepted_at is not None
    assert item_a.status == "ACCEPTED"
    assert item_b.status == "NEW"  # untouched — did not jump ahead


def test_item_reaching_preparing_auto_advances_order_from_accepted(kds_client, table, menu_item, other_menu_item):
    """First item to start cooking advances the ORDER to PREPARING, without
    forcing sibling items (still NEW/ACCEPTED) to jump ahead of their own
    individual progress."""
    _, kitchen = kds_client
    session, _ = table_services.get_or_create_active_session(table.id)
    order = _place_order_with_two_items(session.id, menu_item, other_menu_item)
    item_a, item_b = order.items.all()

    kitchen.patch(f"/v1/orders/{order.id}/status/", {"status": "accepted"}, format="json")

    response = kitchen.patch(
        f"/v1/orders/{order.id}/items/{item_a.id}/status/", {"status": "preparing"}, format="json"
    )
    assert response.status_code == 200, response.data

    order.refresh_from_db()
    item_a.refresh_from_db()
    item_b.refresh_from_db()
    assert order.status == "PREPARING"
    assert item_a.status == "PREPARING"
    assert item_b.status == "ACCEPTED"  # untouched — did not jump ahead


def test_order_level_status_endpoint_still_works_unchanged(kds_client, table, menu_item):
    """Regression guard: a single-item order still walks the whole-order
    lifecycle end to end, with the item now correctly tracking it."""
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
    assert order.items.first().status == "READY"


def test_serving_one_order_does_not_touch_other_tables_orders(
    server_client, restaurant, branch, menu_item
):
    """2026-09-21, per Shereena: "mark Table 8 Served and the other tables'
    orders also change to Served or disappear". She could not reproduce it
    on retest; this pins the actual behaviour down so it can't regress
    silently. Note the 'disappear' half is a separate thing - see
    test_my_orders_keeps_collected_but_drops_served below."""
    from apps.orders import services as order_services
    from apps.tables import services as table_services
    from apps.tables.models import Table

    user, _ = server_client
    user.branch = branch
    user.save(update_fields=["branch"])

    orders = {}
    for number in ("1", "2", "3", "8"):
        t = Table.objects.create(restaurant=restaurant, branch=branch, table_number=number)
        session, _ = table_services.get_or_create_active_session(t.id)
        order = order_services.place_order(
            session.id, [{"menu_item_id": menu_item.id, "quantity": 1}], placed_by=user,
        )
        for step in ("ACCEPTED", "PREPARING", "READY"):
            order_services.advance_kitchen_status(order.id, step)
        order_services.mark_collected(order.id)
        orders[number] = order

    for number, order in orders.items():
        order.refresh_from_db()
        assert order.status == "COLLECTED", f"table {number} should be COLLECTED"

    order_services.mark_served(orders["8"].id)

    orders["8"].refresh_from_db()
    assert orders["8"].status == "SERVED"
    for number in ("1", "2", "3"):
        orders[number].refresh_from_db()
        assert orders[number].status == "COLLECTED", (
            f"table {number} changed to {orders[number].status} when table 8 was served"
        )
        statuses = sorted({i.status for i in orders[number].items.all()})
        assert statuses == ["READY"], (
            f"table {number}'s items became {statuses} when table 8 was served"
        )


def test_my_orders_keeps_collected_but_drops_served(server_client, restaurant, branch, menu_item):
    """The 'orders disappear from the list' half of Shereena's report.
    Until 2026-09-21 My Orders showed only NEW/ACCEPTED/PREPARING/READY,
    so an order vanished the moment it was COLLECTED - one step BEFORE
    serving - which is what made serving one table look like it had hidden
    the others. Per Karwin it now keeps COLLECTED orders: the server is
    still carrying that food. SERVED is still dropped; the job is done."""
    from apps.orders import services as order_services
    from apps.tables import services as table_services
    from apps.tables.models import Table

    user, client = server_client
    user.branch = branch
    user.save(update_fields=["branch"])

    t = Table.objects.create(restaurant=restaurant, branch=branch, table_number="21")
    session, _ = table_services.get_or_create_active_session(t.id)
    order = order_services.place_order(
        session.id, [{"menu_item_id": menu_item.id, "quantity": 1}], placed_by=user,
    )

    def listed():
        return any(o["id"] == str(order.id) for o in client.get("/v1/orders/mine/").data)

    assert listed()
    for step in ("ACCEPTED", "PREPARING", "READY"):
        order_services.advance_kitchen_status(order.id, step)
    assert listed()

    order_services.mark_collected(order.id)
    assert listed(), "a collected order is still in the server's hands"

    order_services.mark_served(order.id)
    assert not listed(), "a served order is finished and should drop off"
