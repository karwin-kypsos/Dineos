from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.orders.models import Order
from apps.tables.models import TableSession
from apps.tables.services import close_session

from .models import Bill, CashierShift


class ShiftAlreadyOpenError(Exception):
    pass


class ShiftAlreadyClosedError(Exception):
    pass


class DiscrepancyNotAcknowledgedError(Exception):
    """Raised when the counted cash doesn't match the system total and the
    caller hasn't set `acknowledge_discrepancy` — mirrors the Figma flow's
    'Discrepancy detected... Go back and recount / Proceed with discrepancy'
    fork instead of silently closing over a mismatch.
    """

    def __init__(self, discrepancy):
        self.discrepancy = discrepancy
        super().__init__(f"Counted cash differs from the system total by {discrepancy}.")


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
        branch=session.table.branch,
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


def _compute_order_totals(order, restaurant):
    subtotal = sum((item.unit_price * item.quantity for item in order.items.all()), Decimal("0"))
    tax_amount = (subtotal * restaurant.gst_percentage / Decimal("100")).quantize(Decimal("0.01"))
    service_charge = (subtotal * restaurant.service_charge_percentage / Decimal("100")).quantize(Decimal("0.01"))
    total_amount = subtotal + tax_amount + service_charge
    return subtotal, tax_amount, service_charge, total_amount


@transaction.atomic
def get_takeaway_bill_preview(order_id):
    order = Order.objects.select_related("branch__restaurant").prefetch_related("items").get(id=order_id)
    restaurant = order.branch.restaurant
    subtotal, tax_amount, service_charge, total_amount = _compute_order_totals(order, restaurant)
    return {
        "order_id": str(order.id),
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "service_charge": service_charge,
        "total_amount": total_amount,
    }


@transaction.atomic
def pay_takeaway_bill(order_id, payment_method, processed_by):
    # of=("self",): Order.branch is nullable, so select_related("branch__restaurant")
    # compiles to a LEFT OUTER JOIN — PostgreSQL rejects a plain FOR UPDATE across
    # the nullable side of an outer join ("FeatureNotSupported"). Restricting the
    # lock to just the orders row (which is all pay_takeaway_bill's idempotency
    # check needs) keeps the join without asking Postgres to lock through it.
    order = (
        Order.objects.select_for_update(of=("self",))
        .select_related("branch__restaurant")
        .prefetch_related("items")
        .get(id=order_id)
    )

    existing_bill = Bill.objects.filter(order=order).first()
    if existing_bill:
        return existing_bill  # idempotent replay

    restaurant = order.branch.restaurant
    subtotal, tax_amount, service_charge, total_amount = _compute_order_totals(order, restaurant)
    bill = Bill.objects.create(
        order=order,
        branch=order.branch,
        subtotal=subtotal,
        tax_amount=tax_amount,
        service_charge=service_charge,
        total_amount=total_amount,
        payment_method=payment_method,
        processed_by=processed_by,
    )

    transaction.on_commit(lambda: _notify_takeaway_payment_confirmed(bill, order, restaurant))
    return bill


def _notify_takeaway_payment_confirmed(bill, order, restaurant):
    if not restaurant.notifications_enabled:
        return

    from apps.notifications.services import notify_role

    notify_role(
        ["ADMIN", "MANAGER"],
        tenant=restaurant,
        type="PAYMENT_CONFIRMED",
        title=f"Payment received — takeaway for {order.customer_name or 'walk-in'}",
        body=f"Bill total: {bill.total_amount}",
    )


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


# ---------------------------------------------------------------------------
# Cashier shifts — "Cashier Home" / "Daily Collections" / "Cash Reconciliation"
# ---------------------------------------------------------------------------

_PAYMENT_METHOD_KEYS = {
    Bill.PaymentMethod.CASH: "cash",
    Bill.PaymentMethod.CARD: "card",
    Bill.PaymentMethod.UPI: "upi",
}


def open_shift(cashier):
    """Idempotent, like `get_or_create_active_session` — re-tapping "Start
    Shift" on an already-open shift just returns it rather than erroring.
    """
    existing = CashierShift.objects.filter(cashier=cashier, status=CashierShift.Status.OPEN).first()
    if existing:
        return existing
    return CashierShift.objects.create(restaurant=cashier.restaurant, branch=cashier.branch, cashier=cashier)


def get_current_shift(cashier):
    return CashierShift.objects.filter(cashier=cashier, status=CashierShift.Status.OPEN).first()


def _shift_bills(shift):
    window_end = shift.closed_at or timezone.now()
    return Bill.objects.filter(processed_by=shift.cashier, paid_at__gte=shift.opened_at, paid_at__lte=window_end)


def shift_totals_by_method(shift):
    """The 'System Totals' section of Cash Reconciliation — Cash/Card/UPI
    subtotals plus the grand total, computed straight from `Bill` records
    rather than a stored snapshot (`Bill.processed_by` + `Bill.paid_at` are
    the single source of truth — see `CashierShift`'s docstring).
    """
    totals = {"cash": Decimal("0"), "card": Decimal("0"), "upi": Decimal("0")}
    for bill in _shift_bills(shift):
        key = _PAYMENT_METHOD_KEYS.get(bill.payment_method)
        if key:
            totals[key] += bill.total_amount
    totals["total"] = totals["cash"] + totals["card"] + totals["upi"]
    return totals


def cashier_dashboard(restaurant, cashier):
    """'Cashier Home' — awaiting/occupied/paid-today counts and lists, plus
    today's collected total for the calling cashier's current shift.
    """
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

    awaiting_sessions = (
        TableSession.objects.filter(table__restaurant=restaurant, status=TableSession.Status.BILL_REQUESTED)
        .select_related("table")
    )
    active_sessions = (
        TableSession.objects.filter(table__restaurant=restaurant, status=TableSession.Status.ACTIVE)
        .select_related("table")
    )
    paid_today_count = _restaurant_bills_qs(restaurant).filter(paid_at__gte=today_start).count()

    shift = get_current_shift(cashier)
    collected_today = shift_totals_by_method(shift)["total"] if shift else Decimal("0")

    def _session_summary(session):
        _, _, _, total = _compute_totals(session)
        return {
            "session_id": str(session.id),
            "table_id": str(session.table_id),
            "table_number": session.table.table_number,
            "total_amount": total,
        }

    return {
        "shift": shift,
        "collected_today": collected_today,
        "awaiting_payment": [_session_summary(s) for s in awaiting_sessions],
        "active_tables": [_session_summary(s) for s in active_sessions],
        "paid_today_count": paid_today_count,
    }


def close_shift(shift, counted_cash, acknowledge_discrepancy=False):
    if shift.status == CashierShift.Status.CLOSED:
        raise ShiftAlreadyClosedError()

    system_cash_total = shift_totals_by_method(shift)["cash"]
    discrepancy = counted_cash - system_cash_total

    if discrepancy != 0 and not acknowledge_discrepancy:
        raise DiscrepancyNotAcknowledgedError(discrepancy)

    shift.counted_cash = counted_cash
    shift.discrepancy_acknowledged = discrepancy != 0
    shift.status = CashierShift.Status.CLOSED
    shift.closed_at = timezone.now()
    shift.save(update_fields=["counted_cash", "discrepancy_acknowledged", "status", "closed_at"])
    return shift


def _restaurant_bills_qs(restaurant):
    # A bill is scoped to this restaurant either via its session (dine-in)
    # or via its order (takeaway, which has no session) — see Bill's
    # "exactly one of session or order" constraint.
    return Bill.objects.filter(
        Q(session__table__restaurant=restaurant) | Q(order__branch__restaurant=restaurant)
    )


def daily_collections(restaurant, date):
    day_start = timezone.make_aware(timezone.datetime.combine(date, timezone.datetime.min.time()))
    day_end = day_start + timezone.timedelta(days=1)
    previous_day_start = day_start - timezone.timedelta(days=1)

    bills = _restaurant_bills_qs(restaurant).filter(paid_at__gte=day_start, paid_at__lt=day_end).select_related(
        "session__table", "order"
    )
    previous_day_total = _restaurant_bills_qs(restaurant).filter(
        paid_at__gte=previous_day_start, paid_at__lt=day_start
    ).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")

    totals = {"cash": Decimal("0"), "card": Decimal("0"), "upi": Decimal("0")}
    bill_amounts = []
    for bill in bills:
        key = _PAYMENT_METHOD_KEYS.get(bill.payment_method)
        if key:
            totals[key] += bill.total_amount
        bill_amounts.append(bill.total_amount)
    grand_total = totals["cash"] + totals["card"] + totals["upi"]

    return {
        "date": date,
        "total_collected": grand_total,
        "vs_yesterday": grand_total - previous_day_total,
        "tables_served": bills.count(),
        "avg_bill_value": (grand_total / len(bill_amounts)) if bill_amounts else Decimal("0"),
        "largest_bill": max(bill_amounts) if bill_amounts else Decimal("0"),
        "smallest_bill": min(bill_amounts) if bill_amounts else Decimal("0"),
        "payment_breakdown": totals,
        "bills": list(bills.order_by("-paid_at")),
    }
