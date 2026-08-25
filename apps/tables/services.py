from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from .models import Table, TableSession


@transaction.atomic
def get_or_create_active_session(table_id):
    """Idempotent: returns the existing open session for this table if one
    exists, otherwise starts a fresh one. Never errors on a double-tap.
    """
    table = Table.objects.select_for_update().get(id=table_id)
    existing = TableSession.objects.filter(table=table, status__in=["ACTIVE", "BILL_REQUESTED"]).first()
    if existing:
        return existing, False

    session = TableSession.objects.create(table=table, status=TableSession.Status.ACTIVE)
    table.status = Table.Status.OCCUPIED
    table.save(update_fields=["status"])
    return session, True


def assign_next_server(session):
    """Round-robin auto-assignment on a session's first order (called from
    apps.orders.services.place_order) — no-op if already assigned, or if
    there are no active Servers to assign to. Rotation state is derived from
    the most recently assigned session rather than a separate counter: find
    where the last-assigned server sits in the ordered Server list for this
    branch, and hand the table to whoever's next, wrapping around.
    """
    if session.assigned_server_id is not None:
        return session

    from django.contrib.auth import get_user_model

    User = get_user_model()
    table = session.table
    servers = list(
        User.objects.filter(
            restaurant=table.restaurant, branch=table.branch, role="SERVER", is_active=True,
        ).order_by("id")
    )
    if not servers:
        return session

    last_assigned = (
        TableSession.objects.filter(table__branch=table.branch, assigned_server__isnull=False)
        .exclude(id=session.id)
        .order_by("-opened_at")
        .first()
    )
    next_server = servers[0]
    if last_assigned is not None and last_assigned.assigned_server in servers:
        idx = servers.index(last_assigned.assigned_server)
        next_server = servers[(idx + 1) % len(servers)]

    session.assigned_server = next_server
    session.save(update_fields=["assigned_server"])
    return session


@transaction.atomic
def request_bill(session_id):
    session = TableSession.objects.select_for_update().get(id=session_id)
    session.status = TableSession.Status.BILL_REQUESTED
    session.bill_requested_at = timezone.now()
    session.save(update_fields=["status", "bill_requested_at"])
    restaurant = session.table.restaurant

    transaction.on_commit(
        lambda: _broadcast(
            restaurant,
            [f"table_{session.table_id}"],
            "table_bill_requested",
            {"session_id": str(session.id), "table_id": str(session.table_id), "table_number": session.table.table_number},
        )
    )
    transaction.on_commit(
        lambda: _broadcast(
            restaurant,
            [f"cashiers_{restaurant.id}", f"managers_{restaurant.id}"],
            "table_bill_requested",
            {"session_id": str(session.id), "table_id": str(session.table_id), "table_number": session.table.table_number},
        )
    )
    transaction.on_commit(lambda: _notify_bill_requested(session, restaurant))
    return session


def _notify_bill_requested(session, restaurant):
    if not restaurant.notifications_enabled:
        return

    from apps.notifications.services import notify_role

    notify_role(
        ["CASHIER", "MANAGER"],
        tenant=restaurant,
        type="BILL_REQUESTED",
        title=f"Bill requested — Table {session.table.table_number}",
        body="Customer is ready to pay.",
        table=session.table,
    )


@transaction.atomic
def close_session(session, reason, closed_by=None):
    """Shared convergence point for both the payment-triggered close and the
    manager-override close. Frees the table and fires the WS events —
    never touches PreparedPortion (that only happens at order-creation time).
    """
    session = TableSession.objects.select_for_update().get(id=session.id)
    table = Table.objects.select_for_update().get(id=session.table_id)
    restaurant = table.restaurant

    session.status = TableSession.Status.CLOSED
    session.closed_at = timezone.now()
    session.closed_by = closed_by
    session.close_reason = reason
    session.save(update_fields=["status", "closed_at", "closed_by", "close_reason"])

    table.status = Table.Status.AVAILABLE
    table.save(update_fields=["status"])

    transaction.on_commit(
        lambda: _broadcast(
            restaurant,
            [f"table_session_{session.id}", f"table_{table.id}", f"staff_all_{restaurant.id}"],
            "table_session_closed",
            {"session_id": str(session.id), "table_id": str(table.id), "reason": reason},
        )
    )
    transaction.on_commit(
        lambda: _broadcast(
            restaurant,
            [f"staff_all_{restaurant.id}", f"table_{table.id}"],
            "table_status_changed",
            {"table_id": str(table.id), "status": table.status},
        )
    )
    return session


@transaction.atomic
def manager_override_status(table_id, status, mark_unpaid=False, manager=None):
    table = Table.objects.select_for_update().get(id=table_id)
    restaurant = table.restaurant
    session = TableSession.objects.filter(table=table, status__in=["ACTIVE", "BILL_REQUESTED"]).first()
    if session and mark_unpaid:
        close_session(session, reason=TableSession.CloseReason.MANAGER_OVERRIDE, closed_by=manager)
    else:
        table.status = status
        table.save(update_fields=["status"])
        transaction.on_commit(
            lambda: _broadcast(
                restaurant,
                [f"staff_all_{restaurant.id}", f"table_{table.id}"],
                "table_status_changed",
                {"table_id": str(table.id), "status": table.status},
            )
        )
    return table


def _broadcast(restaurant, groups, event_type, payload):
    if not restaurant.realtime_enabled:
        return
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    for group in groups:
        async_to_sync(channel_layer.group_send)(group, {"type": event_type, **payload})
