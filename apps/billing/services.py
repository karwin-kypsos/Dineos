from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from apps.orders.models import Order
from apps.tables.models import TableSession
from apps.tables.services import close_session

from .models import Bill


def _compute_totals(session):
    orders = Order.objects.filter(session=session).exclude(status="CANCELLED").prefetch_related("items")
    subtotal = sum((item.unit_price * item.quantity for order in orders for item in order.items.all()), Decimal("0"))

    restaurant = session.table.restaurant
    tax_amount = (subtotal * restaurant.gst_percentage / Decimal("100")).quantize(Decimal("0.01"))
    service_charge = (subtotal * restaurant.service_charge_percentage / Decimal("100")).quantize(Decimal("0.01"))
    total_amount = subtotal + tax_amount + service_charge
    return subtotal, tax_amount, service_charge, total_amount


@transaction.atomic
def get_bill_preview(session_id):
    session = TableSession.objects.get(id=session_id)
    subtotal, tax_amount, service_charge, total_amount = _compute_totals(session)
    return {
        "session_id": str(session.id),
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "service_charge": service_charge,
        "total_amount": total_amount,
    }


@transaction.atomic
def pay_bill(session_id, payment_method, processed_by):
    session = TableSession.objects.select_for_update().get(id=session_id)

    existing_bill = Bill.objects.filter(session=session).first()
    if existing_bill:
        return existing_bill  # idempotent replay — session already paid

    subtotal, tax_amount, service_charge, total_amount = _compute_totals(session)
    bill = Bill.objects.create(
        session=session,
        subtotal=subtotal,
        tax_amount=tax_amount,
        service_charge=service_charge,
        total_amount=total_amount,
        payment_method=payment_method,
        processed_by=processed_by,
    )

    # Prepared portions are NEVER touched here — decrement only happens at
    # order-creation time (apps/orders/services.py::place_order).
    close_session(session, reason=TableSession.CloseReason.PAID, closed_by=processed_by)

    transaction.on_commit(lambda: _broadcast_payment_confirmed(bill, session))
    transaction.on_commit(lambda: _notify_payment_confirmed(bill, session))
    return bill


def _notify_payment_confirmed(bill, session):
    from apps.notifications.services import notify_role

    restaurant = session.table.restaurant
    if not restaurant.notifications_enabled:
        return

    notify_role(
        ["ADMIN", "MANAGER"],
        tenant=restaurant,
        type="PAYMENT_CONFIRMED",
        title=f"Payment received — Table {session.table.table_number}",
        body=f"Bill total: {bill.total_amount}",
        table=session.table,
    )


def _broadcast_payment_confirmed(bill, session):
    restaurant = session.table.restaurant
    if not restaurant.realtime_enabled:
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    payload = {
        "type": "payment_confirmed",
        "bill_id": str(bill.id),
        "session_id": str(session.id),
        "table_id": str(session.table_id),
        "total_amount": str(bill.total_amount),
    }
    for group in (f"cashiers_{restaurant.id}", f"managers_{restaurant.id}", f"table_session_{session.id}"):
        async_to_sync(channel_layer.group_send)(group, payload)
