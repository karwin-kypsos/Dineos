from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.menu.models import MenuItem
from apps.menu.services import decrement_portions
from apps.tables.models import TableSession
from core.exceptions import InvalidStatusTransitionError, SessionNotOpenError

from .models import Order, OrderItem

KITCHEN_NEXT_STATUS = {
    Order.Status.NEW: Order.Status.ACCEPTED,
    Order.Status.ACCEPTED: Order.Status.PREPARING,
    Order.Status.PREPARING: Order.Status.READY,
}
KITCHEN_TIMESTAMP_FIELD = {
    Order.Status.ACCEPTED: "accepted_at",
    Order.Status.READY: "ready_at",
}
# Same transition table drives per-item status — NEW/ACCEPTED/PREPARING/READY
# is exactly Order.ITEM_STATUSES, so KITCHEN_NEXT_STATUS is reused as-is
# rather than duplicated for items.
ORDER_STATUSES_AT_OR_PAST_READY = {
    Order.Status.READY,
    Order.Status.COLLECTED,
    Order.Status.SERVED,
    Order.Status.CANCELLED,
}


class KitchenDisplayNotEnabledError(InvalidStatusTransitionError):
    default_detail = "Kitchen Display is not enabled for this restaurant."


def _order_restaurant(order):
    return order.table.restaurant if order.table_id else order.branch.restaurant


def _order_label(order):
    if order.table_id:
        return f"Table {order.table.table_number}"
    return f"takeaway order for {order.customer_name or 'walk-in customer'}"


def _create_order_items(order, items, restaurant):
    zero_hits = []
    portion_updates = []
    for item in items:
        menu_item = MenuItem.objects.select_for_update().get(
            id=item["menu_item_id"], is_available=True, category__restaurant=restaurant
        )
        OrderItem.objects.create(
            order=order, menu_item=menu_item, quantity=item["quantity"], notes=item.get("notes", ""), unit_price=menu_item.price
        )
        portion, hit_zero = decrement_portions(menu_item, item["quantity"])
        if portion is not None:
            portion_updates.append((menu_item.id, portion.portions_remaining))
            if hit_zero:
                zero_hits.append(menu_item.id)
    return zero_hits, portion_updates


def _finalize_new_order(order, restaurant, portion_updates, zero_hits):
    if not restaurant.kitchen_enabled:
        # No Kitchen Display add-on — skip the kitchen lifecycle entirely.
        # Staff take the order through to served in one action instead.
        order.status = Order.Status.SERVED
        order.served_at = timezone.now()
        order.save(update_fields=["status", "served_at"])
        transaction.on_commit(lambda: _broadcast_status_changed(order, restaurant))
    else:
        transaction.on_commit(lambda: _broadcast_order_placed(order, restaurant, portion_updates, zero_hits))


@transaction.atomic
def place_order(session_id, items, placed_by=None, notes=""):
    session = TableSession.objects.select_for_update().get(id=session_id)
    if session.status not in (TableSession.Status.ACTIVE, TableSession.Status.BILL_REQUESTED):
        raise SessionNotOpenError()
    restaurant = session.table.restaurant

    round_number = Order.objects.filter(session=session).count() + 1
    if round_number == 1:
        from apps.tables.services import assign_next_server

        session = assign_next_server(session)

    order = Order.objects.create(
        order_type=Order.OrderType.DINE_IN, session=session, table=session.table, branch=session.table.branch,
        round_number=round_number, placed_by=placed_by, notes=notes,
    )

    zero_hits, portion_updates = _create_order_items(order, items, restaurant)
    _finalize_new_order(order, restaurant, portion_updates, zero_hits)
    return order


@transaction.atomic
def place_takeaway_order(restaurant, branch, items, customer_name="", customer_phone="", placed_by=None, notes=""):
    """Same lifecycle as a dine-in order (kitchen states, portion
    deduction) but with no table/session — see Order.OrderType.TAKEAWAY."""
    order = Order.objects.create(
        order_type=Order.OrderType.TAKEAWAY, branch=branch,
        customer_name=customer_name, customer_phone=customer_phone,
        placed_by=placed_by, notes=notes,
    )

    zero_hits, portion_updates = _create_order_items(order, items, restaurant)
    _finalize_new_order(order, restaurant, portion_updates, zero_hits)
    return order


def _apply_order_status(order, restaurant, target_status):
    """Shared set-status + timestamp + broadcast/notify body, used both by
    the explicit whole-order kitchen-status transition below and by the
    auto-derived advance-to-READY triggered when every item under an order
    independently reaches READY (see _maybe_auto_advance_order_to_ready).
    The caller is responsible for validating the transition is legal —
    this just applies it."""
    order.status = target_status
    timestamp_field = KITCHEN_TIMESTAMP_FIELD.get(target_status)
    update_fields = ["status"]
    if timestamp_field:
        setattr(order, timestamp_field, timezone.now())
        update_fields.append(timestamp_field)
    order.save(update_fields=update_fields)

    transaction.on_commit(lambda: _broadcast_status_changed(order, restaurant))
    if target_status == Order.Status.READY:
        transaction.on_commit(lambda: _notify_order_ready(order, restaurant))


@transaction.atomic
def advance_kitchen_status(order_id, target_status):
    order = Order.objects.select_for_update().get(id=order_id)
    restaurant = _order_restaurant(order)
    if not restaurant.kitchen_enabled:
        raise KitchenDisplayNotEnabledError()

    expected_next = KITCHEN_NEXT_STATUS.get(order.status)
    if expected_next is None or target_status != expected_next:
        raise InvalidStatusTransitionError(f"Cannot move order from {order.status} to {target_status}.")

    _apply_order_status(order, restaurant, target_status)
    return order


def _maybe_auto_advance_order_to_ready(order, restaurant):
    """Backward-compat bridge between the per-item and whole-order status
    flows: restaurants that never touch per-item status are entirely
    unaffected (this only ever runs from advance_item_kitchen_status,
    right after an item is set to READY). Once every item under the order
    has independently reached READY, the order itself is advanced to READY
    too — unless it's already there or past it (READY/COLLECTED/SERVED/
    CANCELLED), in which case this is a no-op. This intentionally bypasses
    the strict KITCHEN_NEXT_STATUS transition check that advance_kitchen_status
    enforces: it's a derived side-effect of item-level progress, not a
    manual kitchen action, so the order can jump straight from NEW/ACCEPTED
    to READY if its items got there first via the per-item endpoint alone.
    """
    if order.status in ORDER_STATUSES_AT_OR_PAST_READY:
        return
    if order.items.exclude(status=Order.Status.READY).exists():
        return
    _apply_order_status(order, restaurant, Order.Status.READY)


@transaction.atomic
def advance_item_kitchen_status(order, item_id, target_status):
    """Per-item counterpart to advance_kitchen_status. `order` must already
    be tenant-scoped by the caller (see OrderItemKitchenStatusView) — this
    re-fetches it with a row lock and validates the transition against the
    ITEM's own current status, independently of its siblings or the parent
    order's status. Returns (order, item)."""
    order = Order.objects.select_for_update().get(id=order.id)
    restaurant = _order_restaurant(order)
    if not restaurant.kitchen_enabled:
        raise KitchenDisplayNotEnabledError()

    item = get_object_or_404(order.items.select_for_update(), id=item_id)

    expected_next = KITCHEN_NEXT_STATUS.get(item.status)
    if expected_next is None or target_status != expected_next:
        raise InvalidStatusTransitionError(f"Cannot move item from {item.status} to {target_status}.")

    item.status = target_status
    item.save(update_fields=["status"])

    transaction.on_commit(lambda: _broadcast_item_status_changed(item, order, restaurant))

    if target_status == Order.Status.READY:
        _maybe_auto_advance_order_to_ready(order, restaurant)

    return order, item


@transaction.atomic
def mark_collected(order_id):
    order = Order.objects.select_for_update().get(id=order_id)
    restaurant = _order_restaurant(order)
    if not restaurant.kitchen_enabled:
        raise KitchenDisplayNotEnabledError()

    if order.status != Order.Status.READY:
        raise InvalidStatusTransitionError("Only a Ready order can be marked Collected.")
    order.status = Order.Status.COLLECTED
    order.collected_at = timezone.now()
    order.save(update_fields=["status", "collected_at"])

    groups = [f"kitchen_{restaurant.id}"]
    if order.session_id:
        groups.append(f"table_session_{order.session_id}")
    transaction.on_commit(lambda: _broadcast(restaurant, groups, "order_collected", _order_payload(order)))
    return order


@transaction.atomic
def mark_served(order_id):
    order = Order.objects.select_for_update().get(id=order_id)
    restaurant = _order_restaurant(order)

    if restaurant.kitchen_enabled:
        if order.status != Order.Status.COLLECTED:
            raise InvalidStatusTransitionError("Only a Collected order can be marked Served.")
    else:
        # Kitchen Display off: staff can serve directly from NEW/ACCEPTED —
        # there's no kitchen lifecycle to have passed through first.
        if order.status == Order.Status.SERVED:
            raise InvalidStatusTransitionError("Order is already Served.")

    order.status = Order.Status.SERVED
    order.served_at = timezone.now()
    order.save(update_fields=["status", "served_at"])

    groups = [f"servers_{restaurant.id}"]
    if order.session_id:
        groups.append(f"table_session_{order.session_id}")
    transaction.on_commit(lambda: _broadcast(restaurant, groups, "order_served", _order_payload(order)))
    return order


def _order_payload(order):
    return {
        "order_id": str(order.id),
        "session_id": str(order.session_id) if order.session_id else None,
        "table_id": str(order.table_id) if order.table_id else None,
        "order_type": order.order_type,
        "status": order.status,
    }


def _notify_order_ready(order, restaurant):
    if not restaurant.notifications_enabled:
        return

    from apps.notifications.services import notify_role

    notify_role(
        ["SERVER"],
        tenant=restaurant,
        type="ORDER_READY",
        title=f"Order ready — {_order_label(order)}",
        body=f"Round {order.round_number} is ready to collect from the kitchen.",
        order=order,
        table=order.table,
    )


def _broadcast(restaurant, groups, event_type, payload):
    if not restaurant.realtime_enabled:
        return
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    for group in groups:
        async_to_sync(channel_layer.group_send)(group, {"type": event_type, **payload})


def _broadcast_order_placed(order, restaurant, portion_updates, zero_hits):
    _broadcast(restaurant, [f"kitchen_{restaurant.id}"], "order_new", _order_payload(order))
    for menu_item_id, remaining in portion_updates:
        _broadcast(
            restaurant,
            [f"customers_global_{restaurant.id}"],
            "portions_updated",
            {"menu_item_id": str(menu_item_id), "portions_remaining": remaining},
        )
    for menu_item_id in zero_hits:
        _broadcast(restaurant, [f"customers_global_{restaurant.id}"], "portions_zero", {"menu_item_id": str(menu_item_id)})


def _broadcast_status_changed(order, restaurant):
    groups = [f"kitchen_{restaurant.id}", f"servers_{restaurant.id}"]
    if order.session_id:
        groups.append(f"table_session_{order.session_id}")
    _broadcast(restaurant, groups, "order_status_changed", _order_payload(order))


def _item_payload(item, order):
    return {
        "order_id": str(order.id),
        "item_id": str(item.id),
        "status": item.status,
        "menu_item_id": item.menu_item_id,
        "quantity": item.quantity,
    }


def _broadcast_item_status_changed(item, order, restaurant):
    groups = [f"kitchen_{restaurant.id}", f"servers_{restaurant.id}"]
    if order.session_id:
        groups.append(f"table_session_{order.session_id}")
    _broadcast(restaurant, groups, "order_item_status_changed", _item_payload(item, order))
