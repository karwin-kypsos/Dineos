from collections import defaultdict

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
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


@transaction.atomic
def place_order(session_id, items, placed_by=None, notes=""):
    session = TableSession.objects.select_for_update().get(id=session_id)
    if session.status not in (TableSession.Status.ACTIVE, TableSession.Status.BILL_REQUESTED):
        raise SessionNotOpenError()

    round_number = Order.objects.filter(session=session).count() + 1
    order = Order.objects.create(
        session=session, table=session.table, round_number=round_number, placed_by=placed_by, notes=notes
    )

    zero_hits = []
    portion_updates = []
    for item in items:
        menu_item = MenuItem.objects.select_for_update().get(id=item["menu_item_id"], is_available=True)
        OrderItem.objects.create(
            order=order, menu_item=menu_item, quantity=item["quantity"], notes=item.get("notes", ""), unit_price=menu_item.price
        )
        portion, hit_zero = decrement_portions(menu_item, item["quantity"])
        if portion is not None:
            portion_updates.append((menu_item.id, portion.portions_remaining))
            if hit_zero:
                zero_hits.append(menu_item.id)

    transaction.on_commit(lambda: _broadcast_order_placed(order, portion_updates, zero_hits))
    return order


@transaction.atomic
def advance_kitchen_status(order_id, target_status):
    order = Order.objects.select_for_update().get(id=order_id)
    expected_next = KITCHEN_NEXT_STATUS.get(order.status)
    if expected_next is None or target_status != expected_next:
        raise InvalidStatusTransitionError(f"Cannot move order from {order.status} to {target_status}.")

    order.status = target_status
    timestamp_field = KITCHEN_TIMESTAMP_FIELD.get(target_status)
    update_fields = ["status"]
    if timestamp_field:
        setattr(order, timestamp_field, timezone.now())
        update_fields.append(timestamp_field)
    order.save(update_fields=update_fields)

    transaction.on_commit(lambda: _broadcast_status_changed(order))
    if target_status == Order.Status.READY:
        transaction.on_commit(lambda: _notify_order_ready(order))
    return order


@transaction.atomic
def mark_collected(order_id):
    order = Order.objects.select_for_update().get(id=order_id)
    if order.status != Order.Status.READY:
        raise InvalidStatusTransitionError("Only a Ready order can be marked Collected.")
    order.status = Order.Status.COLLECTED
    order.collected_at = timezone.now()
    order.save(update_fields=["status", "collected_at"])

    transaction.on_commit(lambda: _broadcast(["kitchen", f"table_session_{order.session_id}"], "order_collected", _order_payload(order)))
    return order


@transaction.atomic
def mark_served(order_id):
    order = Order.objects.select_for_update().get(id=order_id)
    if order.status != Order.Status.COLLECTED:
        raise InvalidStatusTransitionError("Only a Collected order can be marked Served.")
    order.status = Order.Status.SERVED
    order.served_at = timezone.now()
    order.save(update_fields=["status", "served_at"])

    transaction.on_commit(lambda: _broadcast(["servers", f"table_session_{order.session_id}"], "order_served", _order_payload(order)))
    return order


def _order_payload(order):
    return {"order_id": str(order.id), "session_id": str(order.session_id), "table_id": str(order.table_id), "status": order.status}


def _notify_order_ready(order):
    from apps.notifications.services import notify_role

    notify_role(
        ["SERVER"],
        type="ORDER_READY",
        title=f"Order ready — Table {order.table.table_number}",
        body=f"Round {order.round_number} is ready to collect from the kitchen.",
        order=order,
        table=order.table,
    )


def _broadcast(groups, event_type, payload):
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    for group in groups:
        async_to_sync(channel_layer.group_send)(group, {"type": event_type, **payload})


def _broadcast_order_placed(order, portion_updates, zero_hits):
    _broadcast(["kitchen"], "order_new", _order_payload(order))
    for menu_item_id, remaining in portion_updates:
        _broadcast(["customers_global"], "portions_updated", {"menu_item_id": str(menu_item_id), "portions_remaining": remaining})
    for menu_item_id in zero_hits:
        _broadcast(["customers_global"], "portions_zero", {"menu_item_id": str(menu_item_id)})


def _broadcast_status_changed(order):
    _broadcast(["kitchen", "servers", f"table_session_{order.session_id}"], "order_status_changed", _order_payload(order))
