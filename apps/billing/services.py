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


class DiscrepancyReasonRequiredError(Exception):
    """Raised when there's a mismatch and the caller acknowledged it but
    didn't say why — the drawer-closing flow requires a reason, not just a
    yes/no acknowledgement, so the discrepancy is explainable later.
    """

    def __init__(self, discrepancy):
        self.discrepancy = discrepancy
        super().__init__(f"A reason is required for the {discrepancy} discrepancy.")


def line_items(orders):
    """Every non-cancelled order's items, flattened — reused by both bill
    previews (pre-payment) and receipts (post-payment) so the two never
    drift apart on what a 'line item' looks like."""
    return [
        {
            "menu_item_id": item.menu_item_id,
            "menu_item_name": item.menu_item.name,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "line_total": item.line_total,
        }
        for order in orders for item in order.items.all()
    ]


def receipt_branch_info(branch, restaurant):
    """restaurant is passed explicitly (not derived from branch) because a
    bill can be branch-less (legacy/global table) while still always
    belonging to exactly one restaurant — gst/service-charge % must never
    go missing just because branch is null."""
    return {
        "restaurant_name": restaurant.name,
        "branch_name": branch.name if branch else None,
        "branch_address": branch.address if branch else None,
        "branch_phone": branch.phone if branch else None,
        "gst_percentage": restaurant.gst_percentage,
        "service_charge_percentage": restaurant.service_charge_percentage,
    }


def _compute_totals(session):
    orders = list(Order.objects.filter(session=session).exclude(status="CANCELLED").prefetch_related("items"))
    subtotal = sum((item.unit_price * item.quantity for order in orders for item in order.items.all()), Decimal("0"))

    restaurant = session.table.restaurant
    tax_amount = (subtotal * restaurant.gst_percentage / Decimal("100")).quantize(Decimal("0.01"))
    service_charge = (subtotal * restaurant.service_charge_percentage / Decimal("100")).quantize(Decimal("0.01"))
    total_amount = subtotal + tax_amount + service_charge
    return subtotal, tax_amount, service_charge, total_amount, orders


@transaction.atomic
def get_bill_preview(session_id):
    session = TableSession.objects.select_related("table__branch__restaurant").get(id=session_id)
    subtotal, tax_amount, service_charge, total_amount, orders = _compute_totals(session)
    return {
        "session_id": str(session.id),
        "table_number": session.table.table_number,
        # Never "PAID" here — a paid session returns via BillSerializer
        # instead (see SessionBillView), never this preview.
        "payment_status": "BILL_REQUESTED" if session.status == TableSession.Status.BILL_REQUESTED else "PENDING",
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "service_charge": service_charge,
        "total_amount": total_amount,
        "items": line_items(orders),
        **receipt_branch_info(session.table.branch, session.table.restaurant),
    }


@transaction.atomic
def pay_bill(session_id, payment_method, processed_by, amount_received=None):
    session = TableSession.objects.select_for_update().get(id=session_id)

    existing_bill = Bill.objects.filter(session=session).first()
    if existing_bill:
        return existing_bill  # idempotent replay — session already paid

    subtotal, tax_amount, service_charge, total_amount, _orders = _compute_totals(session)
    change_given = amount_received - total_amount if amount_received is not None else None
    bill = Bill.objects.create(
        session=session,
        branch=session.table.branch,
        subtotal=subtotal,
        tax_amount=tax_amount,
        service_charge=service_charge,
        total_amount=total_amount,
        payment_method=payment_method,
        processed_by=processed_by,
        amount_received=amount_received,
        change_given=change_given,
    )

    # Prepared portions are NEVER touched here — decrement only happens at
    # order-creation time (apps/orders/services.py::place_order).
    close_session(session, reason=TableSession.CloseReason.PAID, closed_by=processed_by)

    transaction.on_commit(lambda: _broadcast_payment_confirmed(bill, session))
    transaction.on_commit(lambda: _notify_payment_confirmed(bill, session))
    return bill


def _takeaway_root(order):
    """Billing is always keyed off the first round (Bill.order is a
    OneToOne) — a later round's id resolves back to it so paying/previewing
    with any round's order_id bills the whole takeaway order, same as
    dine-in bills the whole session regardless of which order the caller
    has in hand."""
    return order if order.parent_order_id is None else order.parent_order


def _compute_order_totals(order, restaurant):
    from apps.orders.services import takeaway_group

    orders = takeaway_group(order)
    subtotal = sum(
        (item.unit_price * item.quantity for round_order in orders for item in round_order.items.all()), Decimal("0")
    )
    tax_amount = (subtotal * restaurant.gst_percentage / Decimal("100")).quantize(Decimal("0.01"))
    service_charge = (subtotal * restaurant.service_charge_percentage / Decimal("100")).quantize(Decimal("0.01"))
    total_amount = subtotal + tax_amount + service_charge
    return subtotal, tax_amount, service_charge, total_amount, orders


@transaction.atomic
def get_takeaway_bill_preview(order_id):
    order = Order.objects.select_related("branch__restaurant").prefetch_related("items", "rounds__items").get(
        id=order_id
    )
    root = _takeaway_root(order)
    restaurant = root.branch.restaurant
    subtotal, tax_amount, service_charge, total_amount, orders = _compute_order_totals(root, restaurant)
    return {
        "order_id": str(root.id),
        # Always "PENDING" — a paid order returns via BillSerializer instead
        # (see TakeawayBillView), never this preview; takeaway has no
        # "bill requested" step the way a dine-in session does.
        "payment_status": "PENDING",
        "subtotal": subtotal,
        "tax_amount": tax_amount,
        "service_charge": service_charge,
        "total_amount": total_amount,
        "items": line_items(orders),
        **receipt_branch_info(root.branch, restaurant),
    }


@transaction.atomic
def pay_takeaway_bill(order_id, payment_method, processed_by, amount_received=None):
    # of=("self",): Order.branch is nullable, so select_related("branch__restaurant")
    # compiles to a LEFT OUTER JOIN — PostgreSQL rejects a plain FOR UPDATE across
    # the nullable side of an outer join ("FeatureNotSupported"). Restricting the
    # lock to just the orders row (which is all pay_takeaway_bill's idempotency
    # check needs) keeps the join without asking Postgres to lock through it.
    order = (
        Order.objects.select_for_update(of=("self",))
        .select_related("branch__restaurant")
        .prefetch_related("items", "rounds__items")
        .get(id=order_id)
    )
    root = _takeaway_root(order)

    existing_bill = Bill.objects.filter(order=root).first()
    if existing_bill:
        return existing_bill  # idempotent replay

    restaurant = root.branch.restaurant
    subtotal, tax_amount, service_charge, total_amount, _orders = _compute_order_totals(root, restaurant)
    change_given = amount_received - total_amount if amount_received is not None else None
    bill = Bill.objects.create(
        order=root,
        branch=root.branch,
        subtotal=subtotal,
        tax_amount=tax_amount,
        service_charge=service_charge,
        total_amount=total_amount,
        payment_method=payment_method,
        processed_by=processed_by,
        amount_received=amount_received,
        change_given=change_given,
    )

    transaction.on_commit(lambda: _notify_takeaway_payment_confirmed(bill, root, restaurant))
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
    if cashier.branch_id is not None:
        awaiting_sessions = awaiting_sessions.filter(table__branch_id=cashier.branch_id)
        active_sessions = active_sessions.filter(table__branch_id=cashier.branch_id)
    paid_today_count = restaurant_bills_qs(restaurant).filter(paid_at__gte=today_start).count()

    shift = get_current_shift(cashier)
    collected_today = shift_totals_by_method(shift)["total"] if shift else Decimal("0")

    def _session_summary(session):
        # item_count/elapsed_seconds/elapsed_formatted (2026-08-23, per
        # Shereena): Cashier Home's active/awaiting-payment table lists
        # needed the same "how long has this been sitting" + "how much is on
        # it" signals the KDS cards already show — see KDSOrderSerializer's
        # identical elapsed_seconds/elapsed_formatted pattern.
        _, _, _, total, orders = _compute_totals(session)
        item_count = sum(item.quantity for order in orders for item in order.items.all())
        elapsed_seconds = max(0, int((timezone.now() - session.opened_at).total_seconds()))
        minutes, seconds = divmod(elapsed_seconds, 60)
        return {
            "session_id": str(session.id),
            "table_id": str(session.table_id),
            "table_number": session.table.table_number,
            "total_amount": total,
            "item_count": item_count,
            "elapsed_seconds": elapsed_seconds,
            "elapsed_formatted": f"{minutes:02d}:{seconds:02d}",
        }

    return {
        "shift": shift,
        "collected_today": collected_today,
        "awaiting_payment": [_session_summary(s) for s in awaiting_sessions],
        "active_tables": [_session_summary(s) for s in active_sessions],
        "paid_today_count": paid_today_count,
    }


def close_shift(shift, counted_cash, acknowledge_discrepancy=False, discrepancy_reason=""):
    if shift.status == CashierShift.Status.CLOSED:
        raise ShiftAlreadyClosedError()

    system_cash_total = shift_totals_by_method(shift)["cash"]
    discrepancy = counted_cash - system_cash_total

    if discrepancy != 0 and not acknowledge_discrepancy:
        raise DiscrepancyNotAcknowledgedError(discrepancy)
    if discrepancy != 0 and not discrepancy_reason.strip():
        raise DiscrepancyReasonRequiredError(discrepancy)

    shift.counted_cash = counted_cash
    shift.discrepancy_acknowledged = discrepancy != 0
    shift.discrepancy_amount = discrepancy
    shift.discrepancy_reason = discrepancy_reason if discrepancy != 0 else ""
    shift.status = CashierShift.Status.CLOSED
    shift.closed_at = timezone.now()
    shift.save(update_fields=[
        "counted_cash", "discrepancy_acknowledged", "discrepancy_amount", "discrepancy_reason",
        "status", "closed_at",
    ])
    return shift


def restaurant_bills_qs(restaurant):
    # A bill is scoped to this restaurant either via its session (dine-in)
    # or via its order (takeaway, which has no session) — see Bill's
    # "exactly one of session or order" constraint.
    return Bill.objects.filter(
        Q(session__table__restaurant=restaurant) | Q(order__branch__restaurant=restaurant)
    )


def list_bills(restaurant, *, date=None, date_from=None, date_to=None, payment_method=None, cashier_id=None, branch=None, search=None):
    """Cashier's bill history/reconciliation screen — every bill across
    every cashier and both order types (dine-in + takeaway), by default
    with no date restriction so 'all bills under a specific cashier' just
    means cashier_id with date left unset. Pass date to scope to one day
    (e.g. 'today's bills' on the Cashier Home screen), or date_from/date_to
    for an inclusive multi-day range (both are local calendar dates).
    """
    bills = restaurant_bills_qs(restaurant).select_related(
        "session__table", "order", "processed_by", "branch"
    )
    if date is not None:
        day_start = timezone.make_aware(timezone.datetime.combine(date, timezone.datetime.min.time()))
        day_end = day_start + timezone.timedelta(days=1)
        bills = bills.filter(paid_at__gte=day_start, paid_at__lt=day_end)
    elif date_from is not None or date_to is not None:
        if date_from is not None:
            range_start = timezone.make_aware(timezone.datetime.combine(date_from, timezone.datetime.min.time()))
            bills = bills.filter(paid_at__gte=range_start)
        if date_to is not None:
            range_end = timezone.make_aware(timezone.datetime.combine(date_to, timezone.datetime.min.time())) + timezone.timedelta(days=1)
            bills = bills.filter(paid_at__lt=range_end)
    if payment_method:
        bills = bills.filter(payment_method=payment_method)
    if cashier_id:
        bills = bills.filter(processed_by_id=cashier_id)
    if branch is not None:
        bills = bills.filter(branch=branch)
    if search:
        bills = bills.filter(
            Q(order__customer_name__icontains=search)
            | Q(order__customer_phone__icontains=search)
            | Q(session__table__table_number__icontains=search)
            | Q(processed_by__name__icontains=search)
        )
    return bills.order_by("-paid_at")


def _peak_hour_window(bills):
    """'Busiest window' (2026-08-25, per Shereena's My Sales mockup): the
    single clock hour with the highest total collected today, formatted
    like "1:00 PM - 2:00 PM". None on a day with no bills."""
    totals_by_hour = {}
    for bill in bills:
        local_paid_at = timezone.localtime(bill.paid_at)
        totals_by_hour[local_paid_at.hour] = totals_by_hour.get(local_paid_at.hour, Decimal("0")) + bill.total_amount
    if not totals_by_hour:
        return None
    peak_hour = max(totals_by_hour, key=totals_by_hour.get)
    end_hour = (peak_hour + 1) % 24
    start_label = timezone.datetime(2000, 1, 1, peak_hour).strftime("%I:%M %p").lstrip("0")
    end_label = timezone.datetime(2000, 1, 1, end_hour).strftime("%I:%M %p").lstrip("0")
    return f"{start_label} - {end_label}"


def daily_collections(
    restaurant, date=None, search=None, payment_method=None, cashier=None, window_start=None, window_end=None
):
    """cashier (2026-08-25, per Shereena's My Sales page) scopes EVERY figure
    here — totals, payment breakdown, peak hour, bill list — to just that
    one cashier's own processed bills, not the whole restaurant's. Omit it
    for the restaurant-wide oversight view (DailyCollectionsView's default).

    window_start/window_end (2026-08-25, per Shereena's follow-up correction:
    My Sales should reflect the cashier's current SHIFT, not the calendar
    day — a shift can start mid-afternoon and run past midnight, so the two
    aren't the same thing) override the calendar-day window entirely — pass
    the shift's own opened_at/now instead of date. See MySalesView.
    """
    if window_start is not None and window_end is not None:
        day_start, day_end = window_start, window_end
        # A shift has no natural "previous day" — compare against the
        # equivalent-length window immediately before this one instead.
        previous_day_start = day_start - (day_end - day_start)
        previous_day_end = day_start
    else:
        if date is None:
            date = timezone.localdate()
        day_start = timezone.make_aware(timezone.datetime.combine(date, timezone.datetime.min.time()))
        day_end = day_start + timezone.timedelta(days=1)
        previous_day_start = day_start - timezone.timedelta(days=1)
        previous_day_end = day_start

    scoped_bills_qs = restaurant_bills_qs(restaurant)
    if cashier is not None:
        scoped_bills_qs = scoped_bills_qs.filter(processed_by=cashier)

    bills = list(
        scoped_bills_qs.filter(paid_at__gte=day_start, paid_at__lt=day_end).select_related(
            "session__table", "order", "processed_by"
        ).prefetch_related("session__orders__items", "order__items")
    )
    previous_day_total = scoped_bills_qs.filter(
        paid_at__gte=previous_day_start, paid_at__lt=previous_day_end
    ).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")

    totals = {"cash": Decimal("0"), "card": Decimal("0"), "upi": Decimal("0")}
    bill_amounts = []
    for bill in bills:
        key = _PAYMENT_METHOD_KEYS.get(bill.payment_method)
        if key:
            totals[key] += bill.total_amount
        bill_amounts.append(bill.total_amount)
    grand_total = totals["cash"] + totals["card"] + totals["upi"]

    def _pct(amount):
        return float((amount / grand_total * 100).quantize(Decimal("0.1"))) if grand_total > 0 else 0.0

    payment_breakdown = {
        "cash": totals["cash"], "card": totals["card"], "upi": totals["upi"],
        "cash_percentage": _pct(totals["cash"]),
        "card_percentage": _pct(totals["card"]),
        "upi_percentage": _pct(totals["upi"]),
    }

    bills_count = len(bills)
    # "Tables" on the My Sales screen = billed tables today (bills_count —
    # already cashier-scoped above when cashier is passed) + tables still
    # actively being served (not yet billed). The still-active count itself
    # stays restaurant-wide even in cashier-scoped mode — an un-billed table
    # isn't "owned" by any cashier yet, there's nothing to scope it to.
    active_tables_count = TableSession.objects.filter(
        table__restaurant=restaurant, status__in=[TableSession.Status.ACTIVE, TableSession.Status.BILL_REQUESTED]
    ).count()

    # search/payment_method (2026-08-25, per Shereena's "Today's Bills"
    # search box + payment-method filter chips) only narrow the returned
    # bill list, never the totals/breakdown/peak-hour tiles above it — those
    # always reflect the FULL (cashier-scoped, if applicable) day regardless.
    result_bills = bills
    if payment_method:
        result_bills = [bill for bill in result_bills if bill.payment_method == payment_method]
    if search:
        search_lower = search.lower()
        result_bills = [
            bill for bill in result_bills
            if (bill.session_id and bill.session.table and search_lower in bill.session.table.table_number.lower())
            or (bill.order_id and bill.order.customer_name and search_lower in bill.order.customer_name.lower())
            or (bill.order_id and bill.order.customer_phone and search_lower in bill.order.customer_phone.lower())
            or (bill.processed_by and search_lower in bill.processed_by.name.lower())
            or search_lower in bill.payment_method.lower()
        ]

    return {
        "date": date or timezone.localtime(day_start).date(),
        "total_collected": grand_total,
        "vs_yesterday": grand_total - previous_day_total,
        "tables_served": bills_count,
        "tables_count": bills_count + active_tables_count,
        "avg_bill_value": (grand_total / len(bill_amounts)) if bill_amounts else Decimal("0"),
        "largest_bill": max(bill_amounts) if bill_amounts else Decimal("0"),
        "smallest_bill": min(bill_amounts) if bill_amounts else Decimal("0"),
        "peak_hour": _peak_hour_window(bills),
        "payment_breakdown": payment_breakdown,
        "bills": sorted(result_bills, key=lambda b: b.paid_at, reverse=True),
    }
