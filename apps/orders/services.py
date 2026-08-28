from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.menu.models import MenuItem
from apps.menu.services import decrement_portions
from apps.tables.models import TableSession
from core.exceptions import (
    InvalidStatusTransitionError,
    MenuItemNotFoundError,
    OrderAlreadyBilledError,
    SessionNotOpenError,
)

from .models import Order, OrderItem

KITCHEN_NEXT_STATUS = {
    Order.Status.NEW: Order.Status.ACCEPTED,
    Order.Status.ACCEPTED: Order.Status.PREPARING,
    Order.Status.PREPARING: Order.Status.READY,
}
KITCHEN_TIMESTAMP_FIELD = {
    Order.Status.ACCEPTED: "accepted_at",
    Order.Status.PREPARING: "preparing_at",
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
ITEM_STATUS_ORDER = list(Order.ITEM_STATUSES)  # NEW, ACCEPTED, PREPARING, READY


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
        try:
            menu_item = MenuItem.objects.select_for_update().get(
                id=item["menu_item_id"], is_available=True, category__restaurant=restaurant
            )
        except MenuItem.DoesNotExist:
            raise MenuItemNotFoundError(
                f"Menu item {item['menu_item_id']} doesn't exist, isn't available, or doesn't belong to this restaurant."
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

        session = assign_next_server(session, preferred_server=placed_by)

    order = Order.objects.create(
        order_type=Order.OrderType.DINE_IN, session=session, table=session.table, branch=session.table.branch,
        round_number=round_number, placed_by=placed_by, notes=notes,
    )

    zero_hits, portion_updates = _create_order_items(order, items, restaurant)
    _finalize_new_order(order, restaurant, portion_updates, zero_hits)
    return order


def takeaway_group(root):
    """root + every round placed against it, oldest first — the takeaway
    equivalent of `Order.objects.filter(session=session)` for dine-in."""
    return [root, *root.rounds.order_by("round_number")]


@transaction.atomic
def place_takeaway_order(
    restaurant, branch, items, customer_name="", customer_phone="", placed_by=None, notes="", existing_order_id=None
):
    """Same lifecycle as a dine-in order (kitchen states, portion
    deduction) but with no table/session — see Order.OrderType.TAKEAWAY.

    existing_order_id is the takeaway analogue of dine-in's session_id reuse
    (see place_order above): pass the id of an order already placed for this
    customer to add a new round of items to it instead of starting a
    separate, unrelated order. A brand-new Order row is still created for
    the round (so the kitchen sees it as fresh, unprepared work — mirrors
    why dine-in gives each round its own Order rather than appending items
    to one that may already be READY/COLLECTED), but it's linked back to the
    first order via parent_order so it bills as one order (see
    apps.billing.services.get_takeaway_bill_preview/pay_takeaway_bill) and
    shows up together via GET /v1/orders/takeaway/{order_id}/details/.
    """
    parent = None
    round_number = 1
    if existing_order_id is not None:
        from rest_framework.exceptions import PermissionDenied

        from apps.billing.models import Bill

        try:
            existing = Order.objects.select_for_update().get(
                id=existing_order_id, order_type=Order.OrderType.TAKEAWAY, branch__restaurant=restaurant
            )
        except Order.DoesNotExist:
            raise PermissionDenied("This order does not belong to your restaurant.")
        root = existing if existing.parent_order_id is None else existing.parent_order
        if Bill.objects.filter(order=root).exists():
            raise OrderAlreadyBilledError()
        parent = root
        customer_name = customer_name or root.customer_name
        customer_phone = customer_phone or root.customer_phone
        round_number = len(takeaway_group(root)) + 1

    order = Order.objects.create(
        order_type=Order.OrderType.TAKEAWAY, branch=branch,
        customer_name=customer_name, customer_phone=customer_phone,
        parent_order=parent, round_number=round_number,
        placed_by=placed_by, notes=notes,
    )

    zero_hits, portion_updates = _create_order_items(order, items, restaurant)
    _finalize_new_order(order, restaurant, portion_updates, zero_hits)
    return order


def _cascade_items_forward(order, target_status):
    """Bump every item still behind target_status up to it — never touches
    an item already at or past target_status (e.g. one already advanced
    ahead via the per-item endpoint stays exactly where it is, it's never
    downgraded). Only meaningful for the three kitchen-prep item statuses;
    a no-op for anything else (NEW/COLLECTED/SERVED/CANCELLED)."""
    if target_status not in ITEM_STATUS_ORDER:
        return
    behind = ITEM_STATUS_ORDER[: ITEM_STATUS_ORDER.index(target_status)]
    order.items.filter(status__in=behind).update(status=target_status)


def _apply_order_status(order, restaurant, target_status, *, cascade_items=True):
    """Shared set-status + timestamp + broadcast/notify body, used both by
    the explicit whole-order kitchen-status transition below and by the
    auto-derived advances triggered by item-level progress (see
    _maybe_auto_advance_order). The caller is responsible for validating
    the transition is legal — this just applies it.

    cascade_items=True (the explicit whole-order PATCH .../status/ path)
    forward-fills every item that hasn't independently caught up yet — e.g.
    tapping "Accept Order" moves every item NEW→ACCEPTED in the same
    transaction, tapping "Mark Order Ready" moves every remaining item to
    READY. cascade_items=False (the item-driven auto-advance path) leaves
    sibling items exactly as they are — one item reaching PREPARING advances
    the ORDER to PREPARING without forcing its still-NEW/ACCEPTED siblings
    to jump ahead of their own individual progress.
    """
    order.status = target_status
    timestamp_field = KITCHEN_TIMESTAMP_FIELD.get(target_status)
    update_fields = ["status"]
    if timestamp_field:
        setattr(order, timestamp_field, timezone.now())
        update_fields.append(timestamp_field)
    order.save(update_fields=update_fields)
    if cascade_items:
        _cascade_items_forward(order, target_status)

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


def _maybe_auto_advance_order(order, restaurant, item_target_status):
    """Bridge between the per-item and whole-order status flows: restaurants
    that never touch per-item status are entirely unaffected (this only
    ever runs from advance_item_kitchen_status, right after one item's
    status changes). Both derived advances intentionally bypass the strict
    KITCHEN_NEXT_STATUS transition check that advance_kitchen_status
    enforces — they're a side-effect of item-level progress, not a manual
    kitchen action — and both call _apply_order_status with
    cascade_items=False so a lone item's progress never forces its
    siblings to jump ahead of their own individual state.

    - item → PREPARING: the moment the first item starts cooking, the
      order itself auto-advances ACCEPTED → PREPARING (only from ACCEPTED —
      if the order is still NEW, e.g. a stray item update the kitchen
      hasn't accepted the ticket for yet, this is a no-op).
    - item → READY: once every item under the order has independently
      reached READY, the order itself advances to READY too — unless
      it's already there or past it (READY/COLLECTED/SERVED/CANCELLED).
    """
    if order.status in ORDER_STATUSES_AT_OR_PAST_READY:
        return
    if item_target_status == Order.Status.PREPARING:
        if order.status == Order.Status.ACCEPTED:
            _apply_order_status(order, restaurant, Order.Status.PREPARING, cascade_items=False)
    elif item_target_status == Order.Status.READY:
        if not order.items.exclude(status=Order.Status.READY).exists():
            _apply_order_status(order, restaurant, Order.Status.READY, cascade_items=False)


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

    if target_status in (Order.Status.PREPARING, Order.Status.READY):
        _maybe_auto_advance_order(order, restaurant, target_status)

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

    # Takeaway has no table/assigned server (round-robin assignment is
    # dine-in only — see assign_next_server), so "notify SERVER" for a
    # takeaway order used to ping every server restaurant-wide about
    # something none of them could act on, while the Cashier — who's the
    # one actually handing it over / collecting payment — got nothing
    # (2026-08-26, per Shereena). Dine-in keeps notifying the Server.
    roles = ["CASHIER"] if order.order_type == Order.OrderType.TAKEAWAY else ["SERVER"]
    notify_role(
        roles,
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
    # Bug (2026-08-27, per Manikandan's testing): this only ever notified
    # the kitchen channel, never servers_{restaurant.id} — unlike every
    # other broadcast in this file (_broadcast_status_changed,
    # _broadcast_item_status_changed both include it. A brand-new order
    # (status=NEW, before the kitchen ever touches it) never reached a
    # server's live view in real time; they'd only find out once the
    # kitchen advanced its status. Matches "Server can't see customer
    # orders correctly" — this applies to every new order regardless of
    # who placed it, not just customer ones, since the gap was in the
    # broadcast itself, not in who triggered it.
    _broadcast(restaurant, [f"kitchen_{restaurant.id}", f"servers_{restaurant.id}"], "order_new", _order_payload(order))
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
    # session_id added + item_id kept as a plain int, not a string (2026-08-28,
    # per Shereena's exact payload spec via Telegram) — item_id is a plain
    # AutoField, not a UUID like order_id/table_id, so it doesn't need
    # stringifying, and the Staff feed needs session_id here the same way
    # order_status_changed already carries it, to resolve which table/session
    # an item update belongs to without a second lookup.
    return {
        "order_id": str(order.id),
        "session_id": str(order.session_id) if order.session_id else None,
        "item_id": item.id,
        "status": item.status,
        "menu_item_id": item.menu_item_id,
        "quantity": item.quantity,
    }


def _broadcast_item_status_changed(item, order, restaurant):
    groups = [f"kitchen_{restaurant.id}", f"servers_{restaurant.id}"]
    if order.session_id:
        groups.append(f"table_session_{order.session_id}")
    _broadcast(restaurant, groups, "order_item_status_changed", _item_payload(item, order))
