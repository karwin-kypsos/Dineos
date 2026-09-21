import pytest

from apps.orders import services as order_services
from apps.tables import services as table_services

pytestmark = pytest.mark.django_db


def test_new_order_broadcasts_to_servers_group_not_just_kitchen(
    django_capture_on_commit_callbacks, api_client, table, menu_item, restaurant, monkeypatch
):
    """Regression (2026-08-27, Manikandan): a brand-new order (status=NEW,
    before the kitchen ever touches it) only ever broadcast to the kitchen
    channel, never servers_{restaurant.id} -- unlike every status-change
    broadcast, which includes both. A server's live view never found out
    about a new order in real time until the kitchen advanced its status.
    Matches "Server can't see customer orders correctly".
    """
    calls = []
    original_broadcast = order_services._broadcast

    def _spy(restaurant_arg, groups, event_type, payload):
        calls.append((groups, event_type))
        return original_broadcast(restaurant_arg, groups, event_type, payload)

    monkeypatch.setattr(order_services, "_broadcast", _spy)

    session, _ = table_services.get_or_create_active_session(table.id)
    with django_capture_on_commit_callbacks(execute=True):
        order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])

    order_new_calls = [groups for groups, event_type in calls if event_type == "order_new"]
    assert len(order_new_calls) == 1
    assert f"servers_{restaurant.id}" in order_new_calls[0]
    assert f"kitchen_{restaurant.id}" in order_new_calls[0]


def test_item_status_changed_payload_includes_session_id_and_plain_int_item_id(
    django_capture_on_commit_callbacks, api_client, table, menu_item, restaurant, monkeypatch,
):
    """Regression (2026-08-28, Shereena's exact payload spec via Telegram):
    order_item_status_changed was missing session_id (order_status_changed
    already had it) and stringified item_id -- OrderItem's id is a plain
    AutoField, not a UUID, so it should serialize as int."""
    payloads = []
    original_broadcast = order_services._broadcast

    def _spy(restaurant_arg, groups, event_type, payload):
        if event_type == "order_item_status_changed":
            payloads.append(payload)
        return original_broadcast(restaurant_arg, groups, event_type, payload)

    monkeypatch.setattr(order_services, "_broadcast", _spy)

    session, _ = table_services.get_or_create_active_session(table.id)
    with django_capture_on_commit_callbacks(execute=True):
        order = order_services.place_order(session.id, [{"menu_item_id": menu_item.id, "quantity": 1}])
    item = order.items.first()

    with django_capture_on_commit_callbacks(execute=True):
        order_services.advance_item_kitchen_status(order, item.id, "ACCEPTED")

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["session_id"] == str(session.id)
    assert payload["item_id"] == item.id
    assert isinstance(payload["item_id"], int)


def test_takeaway_status_changed_reaches_cashiers_group(
    django_capture_on_commit_callbacks, restaurant, branch, cashier_client, menu_item, monkeypatch,
):
    """Regression (2026-08-28, Shereena — "this also face in cashier", same
    gap as the Server realtime feed): a takeaway order has no table/session,
    so it never reached any staff group when its status changed — dine-in
    orders get table_session_{session_id}, but takeaway got nothing
    equivalent. The Cashier's own Take Away queue needs cashiers_{id}."""
    cashier_user, _ = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])

    calls = []
    original_broadcast = order_services._broadcast

    def _spy(restaurant_arg, groups, event_type, payload):
        if event_type == "order_status_changed":
            calls.append(groups)
        return original_broadcast(restaurant_arg, groups, event_type, payload)

    monkeypatch.setattr(order_services, "_broadcast", _spy)

    order = order_services.place_takeaway_order(
        restaurant, branch, [{"menu_item_id": menu_item.id, "quantity": 1}], placed_by=cashier_user,
    )
    with django_capture_on_commit_callbacks(execute=True):
        order_services.advance_kitchen_status(order.id, "ACCEPTED")

    assert len(calls) == 1
    # Asserted as an audience, not as literal group names: since 2026-09-21
    # overlapping staff groups are collapsed to whichever single group
    # reaches the same people exactly once (servers + cashiers is everyone,
    # i.e. staff_all), so pinning the name here would fail on a change that
    # keeps every recipient. What matters is that a Cashier and a Server
    # each still hear it, exactly once.
    from apps.websockets.groups import ROLES_IN_GROUP

    reached = [ROLES_IN_GROUP[g.rsplit("_", 1)[0]] for g in calls[0] if g.rsplit("_", 1)[0] in ROLES_IN_GROUP]
    assert sum("CASHIER" in roles for roles in reached) == 1
    assert sum("SERVER" in roles for roles in reached) == 1
    assert sum("MANAGER" in roles for roles in reached) == 1


def test_takeaway_payment_broadcasts_payment_confirmed(cashier_client, branch, menu_item, settings):
    """2026-09-21, per Shereena's "dashboard revenue stays stale after a
    payment": dine-in payments always fired a live payment_confirmed
    event, but takeaway ones only sent a notification - so a screen
    refreshing on payment events never heard about takeaway revenue."""
    from unittest.mock import patch

    from apps.billing import services as billing_services
    from apps.orders import services as order_services

    cashier_user, _ = cashier_client
    cashier_user.branch = branch
    cashier_user.save(update_fields=["branch"])

    order = order_services.place_takeaway_order(
        cashier_user.restaurant, branch, [{"menu_item_id": menu_item.id, "quantity": 1}],
        placed_by=cashier_user,
    )

    with patch("apps.billing.services.async_to_sync") as mock_async:
        bill = billing_services.pay_takeaway_bill(order.id, "CASH", cashier_user)
        billing_services._broadcast_takeaway_payment_confirmed(bill, order, cashier_user.restaurant)

    sent_groups = [c.args[0] for c in mock_async.return_value.call_args_list]
    payloads = [c.args[1] for c in mock_async.return_value.call_args_list]
    assert any(g == f"cashiers_{cashier_user.restaurant.id}" for g in sent_groups)
    assert any(p.get("type") == "payment_confirmed" and p.get("order_id") == str(order.id) for p in payloads)


def test_opening_a_session_announces_the_table_as_occupied(
    django_capture_on_commit_callbacks, table, restaurant, monkeypatch,
):
    """2026-09-21, per Shereena's "Table Overview remains stale after
    real-time events": closing a session has always broadcast
    table_status_changed, but opening one broadcast nothing — a customer
    scanning the QR flipped the table to OCCUPIED with no staff screen
    told about it.
    """
    calls = []
    original = table_services._broadcast

    def _spy(restaurant_arg, groups, event_type, payload):
        calls.append((groups, event_type, payload))
        return original(restaurant_arg, groups, event_type, payload)

    monkeypatch.setattr(table_services, "_broadcast", _spy)

    with django_capture_on_commit_callbacks(execute=True):
        session, created = table_services.get_or_create_active_session(table.id)
    assert created is True

    status_calls = [(g, p) for g, e, p in calls if e == "table_status_changed"]
    assert len(status_calls) == 1, calls
    groups, payload = status_calls[0]
    assert f"staff_all_{restaurant.id}" in groups
    assert f"table_{table.id}" in groups
    assert payload == {"table_id": str(table.id), "status": "OCCUPIED"}

    # Idempotent re-entry must not announce anything a second time.
    calls.clear()
    with django_capture_on_commit_callbacks(execute=True):
        again, created_again = table_services.get_or_create_active_session(table.id)
    assert created_again is False
    assert again.id == session.id
    assert [e for _, e, _ in calls if e == "table_status_changed"] == []
