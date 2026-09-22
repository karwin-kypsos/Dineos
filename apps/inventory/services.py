import json
from datetime import timedelta
from decimal import Decimal

from django.db import models as dj_models
from django.db import transaction
from django.utils import timezone

from apps.notifications.services import notify_role

from .models import (
    AIInsight,
    GoodsReceipt,
    GoodsReceiptLine,
    Ingredient,
    PurchaseOrder,
    PurchaseOrderLine,
    StockMovement,
)


class InsufficientStockError(Exception):
    pass


def _notify_if_stock_level_dropped(ingredient, previous_status):
    """Fire once, on the transition into a worse stock level.

    Keyed on the status BEFORE the deduction rather than a bare
    "is it low" boolean, so each threshold announces itself exactly once
    and staying at that level does not re-alert on every subsequent
    wastage or sale.

    History, because this has moved twice. Originally low and critical
    both fired LOW_STOCK. On 2026-09-14 Karwin suppressed critical to
    stop the double alert - but that left the worst case, running out
    completely, silent, and a single large deduction from healthy
    straight to zero produced nothing at all. On 2026-09-21 he asked for
    a distinct CRITICAL_STOCK alert, which is what this now does:

        healthy -> low       LOW_STOCK
        healthy -> critical  CRITICAL_STOCK   (skipped low entirely)
        low     -> critical  CRITICAL_STOCK
        critical -> critical nothing
        anything going UP    nothing (restocking is not an alert)
    """
    current = ingredient.stock_status
    if current == previous_status or current == "healthy":
        return
    # Only ever alert on the way down. A restock that lands an ingredient
    # back on "low" from "critical" is good news, not an alert.
    RANK = {"healthy": 0, "low": 1, "critical": 2}
    if RANK[current] <= RANK.get(previous_status, 0):
        return

    if current == "critical":
        notify_role(
            ["ADMIN", "MANAGER"], tenant=ingredient.restaurant, type="CRITICAL_STOCK",
            title=f"Out of stock: {ingredient.name}",
            body=f"{ingredient.name} has run out (minimum {ingredient.minimum_stock_level} {ingredient.unit}).",
            data={"ingredient_id": str(ingredient.id)}, branch=ingredient.branch,
        )
        return

    notify_role(
        ["ADMIN", "MANAGER"], tenant=ingredient.restaurant, type="LOW_STOCK",
        title=f"Low stock: {ingredient.name}",
        body=f"{ingredient.current_stock} {ingredient.unit} remaining (minimum {ingredient.minimum_stock_level}).",
        data={"ingredient_id": str(ingredient.id)}, branch=ingredient.branch,
    )


@transaction.atomic
def add_stock(ingredient_id, quantity, unit_cost=None, recorded_by=None, adjustment_reason=""):
    """Raise an ingredient's stock and log the movement.

    2026-09-21: every caller now says WHY. A hand-entered correction and
    a real delivery both raise stock, and without a reason on the row
    they are indistinguishable afterwards - which is exactly the audit
    gap the goods-receipt work is meant to close. record_goods_receipt
    passes GOODS_RECEIPT; the manual endpoint makes the user choose.
    """
    ingredient = Ingredient.objects.select_for_update().get(id=ingredient_id)
    ingredient.current_stock += quantity
    if unit_cost is not None:
        ingredient.unit_cost = unit_cost
    ingredient.save(update_fields=["current_stock", "unit_cost"])
    return StockMovement.objects.create(
        ingredient=ingredient, movement_type=StockMovement.MovementType.RESTOCK,
        quantity=quantity, unit_cost_at_time=unit_cost or ingredient.unit_cost, recorded_by=recorded_by,
        adjustment_reason=adjustment_reason,
    )


@transaction.atomic
def record_wastage(ingredient_id, quantity, wastage_reason, reason="", recorded_by=None):
    ingredient = Ingredient.objects.select_for_update().get(id=ingredient_id)
    # 2026-09-21: compare against max(stock, 0). Prep-log can now drive
    # stock negative, and a bare `quantity > current_stock` would mean
    # that once an ingredient sits at -8 EVERY wastage entry is refused
    # (any positive amount exceeds -8), locking the ingredient out of
    # wastage entirely until someone restocks. Wastage still rejects
    # over-deduction as Karwin wants; it just measures against an empty
    # shelf rather than against a debt.
    on_hand = max(ingredient.current_stock, Decimal("0"))
    if quantity > on_hand:
        raise InsufficientStockError(
            f"Cannot record {quantity} {ingredient.unit} of wastage — only {on_hand} in stock."
        )
    previous_status = ingredient.stock_status
    ingredient.current_stock -= quantity
    ingredient.save(update_fields=["current_stock"])
    _notify_if_stock_level_dropped(ingredient, previous_status)
    return StockMovement.objects.create(
        ingredient=ingredient, movement_type=StockMovement.MovementType.WASTAGE,
        quantity=quantity, wastage_reason=wastage_reason, reason=reason,
        unit_cost_at_time=ingredient.unit_cost, recorded_by=recorded_by,
    )


@transaction.atomic
def deduct_for_usage(ingredient_id, quantity, recorded_by=None):
    """Stock consumed by cooking - the Daily Prep Log's per-recipe deduction.

    2026-09-21: the zero clamp is GONE and stock is allowed to go
    negative. History, because this has now flipped twice. It used to go
    negative; on 2026-08-31 Shereena reported that as a bug (25 on hand
    minus 30 left -5, and adding 10 back gave 5 rather than 10, reading
    as a phantom debt) so it was floored at 0. But restock is additive,
    which means flooring silently DISCARDS the overdraft: the balance
    stops being the truth and never catches up. Per Shereena and Karwin
    the clamp is removed - with additive restock the arithmetic
    self-corrects, -8 + 10 = 2, which is genuinely what is on the shelf.

    This path NEVER rejects, unlike record_wastage. The food is already
    cooked by the time this is called, and refusing would leave
    portions_remaining wrong and the dish unsellable on the POS.

    Returns (movement, warning) - warning is a dict when this deduction
    pushed the ingredient to zero or below, else None, so the caller can
    surface it without a second query.
    """
    ingredient = Ingredient.objects.select_for_update().get(id=ingredient_id)
    available_before = ingredient.current_stock
    previous_status = ingredient.stock_status
    ingredient.current_stock = ingredient.current_stock - quantity
    ingredient.save(update_fields=["current_stock"])
    _notify_if_stock_level_dropped(ingredient, previous_status)

    movement = StockMovement.objects.create(
        ingredient=ingredient, movement_type=StockMovement.MovementType.USAGE,
        quantity=quantity, unit_cost_at_time=ingredient.unit_cost, recorded_by=recorded_by,
    )
    warning = None
    if ingredient.current_stock <= 0:
        # current_stock and available_before are quantised to the
        # column's own 2 places. Recipe quantities carry 3 (0.125 kg is
        # a legitimate per-serving amount), so the arithmetic yields
        # "-8.000" in memory while GET /v1/inventory/ingredients/ says
        # "-8.00" for the very same value - the app would render one
        # number two ways. `requested` keeps its full precision because
        # it is the real deduction, and rounding that WOULD misreport.
        cents = Decimal("0.01")
        warning = {
            "ingredient_id": str(ingredient.id),
            "ingredient_name": ingredient.name,
            "unit": ingredient.unit,
            "requested": str(quantity),
            "available_before": str(available_before.quantize(cents)),
            "current_stock": str(ingredient.current_stock.quantize(cents)),
        }
    return movement, warning


@transaction.atomic
def create_purchase_order(
    restaurant, branch, lines, supplier_name="", supplier_notes="", requested_by=None,
    reason="", is_emergency=False,
):
    po = PurchaseOrder.objects.create(
        restaurant=restaurant, branch=branch, supplier_name=supplier_name,
        supplier_notes=supplier_notes, requested_by=requested_by, reason=reason, is_emergency=is_emergency,
    )
    for line in lines:
        PurchaseOrderLine.objects.create(
            purchase_order=po, ingredient=line["ingredient"],
            quantity_ordered=line["quantity_ordered"], unit_cost=line.get("unit_cost"),
        )

    if is_emergency:
        # Already physically bought - nothing left to approve for something
        # that already happened. 2026-09-21: this used to add stock
        # directly, which was a second code path that could write stock
        # and broke the one rule this whole feature rests on. It now goes
        # through a real goods receipt like every other delivery, so the
        # invariant holds with no exceptions and the emergency restock
        # appears in receipt history rather than materialising from
        # nowhere.
        po.status = PurchaseOrder.Status.APPROVED
        po.approved_by = requested_by
        po.approved_at = timezone.now()
        po.save(update_fields=["status", "approved_by", "approved_at"])
        lines = list(po.lines.select_related("ingredient"))
        for line in lines:
            line.approved_quantity = line.quantity_ordered
            line.save(update_fields=["approved_quantity"])
        record_goods_receipt(
            po.id,
            [{"line": line, "received_quantity": line.quantity_ordered} for line in lines],
            received_by=requested_by,
            notes="Emergency purchase - goods already in hand at the time the request was raised.",
        )
        po.refresh_from_db()
    else:
        # 2026-09-10, per Shereena - Admin needs to know a Manager raised a
        # PO that needs approval. Skipped for emergency POs above since
        # those are already RECEIVED by the time this runs - nothing left
        # for Admin to approve/reject.
        _notify_purchase_order_raised(po)

    return po


def _notify_purchase_order_raised(po):
    transaction.on_commit(lambda: notify_role(
        ["ADMIN"], tenant=po.restaurant, type="PURCHASE_ORDER_RAISED",
        title=f"Purchase order raised — {po.supplier_name or 'no supplier set'}",
        body=f"Requested by {po.requested_by.name if po.requested_by else 'a manager'}.",
        data={"purchase_order_id": str(po.id)}, branch=po.branch,
    ))


def _notify_purchase_order_requester(po, type, title, body):
    # 2026-09-10, per Shereena - the Manager who raised it should hear back
    # once Admin acts on it. Targets that one specific requester directly
    # (notify(), not notify_role()) - this is about their own PO, not
    # something every Manager at the branch needs to see.
    if po.requested_by is None:
        return
    from apps.notifications.services import notify

    transaction.on_commit(lambda: notify(
        po.requested_by, type=type, title=title, body=body,
        data={"purchase_order_id": str(po.id)}, branch=po.branch,
    ))


class OverDeliveryError(Exception):
    """Received more than was approved, without confirm_overdelivery set."""


@transaction.atomic
def approve_purchase_order(po_id, approved_by, items=None, note=""):
    """Approve a PO, optionally cutting individual lines down.

    2026-09-21: approval is now per line. `items` is a list of
    {"line": PurchaseOrderLine, "approved_quantity": Decimal}; any line
    not mentioned is approved at the full requested quantity, which keeps
    the old "approve the whole thing" call working as a no-argument call.
    A quantity of 0 approves nothing for that line - legitimate when a
    supplier cannot source one item at all.
    """
    from django.utils import timezone

    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    if po.status != PurchaseOrder.Status.PENDING_APPROVAL:
        raise ValueError(f"Cannot approve a purchase order in {po.status} status.")

    requested = {line.id: line for line in po.lines.select_for_update()}
    approved = {}
    for entry in items or []:
        line = entry["line"]
        if line.id not in requested:
            raise ValueError("That line does not belong to this purchase order.")
        qty = entry["approved_quantity"]
        if qty < 0:
            raise ValueError("An approved quantity cannot be negative.")
        if qty > line.quantity_ordered:
            raise ValueError(
                f"Cannot approve {qty} of {line.ingredient.name} - only {line.quantity_ordered} was requested."
            )
        approved[line.id] = qty

    for line_id, line in requested.items():
        line.approved_quantity = approved.get(line_id, line.quantity_ordered)
        line.save(update_fields=["approved_quantity"])

    po.status = PurchaseOrder.Status.APPROVED
    po.approved_by = approved_by
    po.approved_at = timezone.now()
    po.approval_note = note or ""
    po.save(update_fields=["status", "approved_by", "approved_at", "approval_note"])
    _notify_purchase_order_requester(
        po, "PURCHASE_ORDER_APPROVED", "Purchase order approved",
        f"Your purchase order ({po.supplier_name or 'no supplier set'}) was approved.",
    )
    return po


@transaction.atomic
def reject_purchase_order(po_id, rejected_by):
    from django.utils import timezone

    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    if po.status != PurchaseOrder.Status.PENDING_APPROVAL:
        raise ValueError(f"Cannot reject a purchase order in {po.status} status.")
    po.status = PurchaseOrder.Status.REJECTED
    po.approved_by = rejected_by
    po.approved_at = timezone.now()
    po.save(update_fields=["status", "approved_by", "approved_at"])
    _notify_purchase_order_requester(
        po, "PURCHASE_ORDER_REJECTED", "Purchase order rejected",
        f"Your purchase order ({po.supplier_name or 'no supplier set'}) was rejected.",
    )
    return po


@transaction.atomic
def record_goods_receipt(po_id, items, received_by=None, confirm_overdelivery=False, notes=""):
    """Record one delivery against a PO. THE ONLY PATH THAT RAISES STOCK.

    `items` is [{"line": PurchaseOrderLine, "received_quantity": Decimal,
    "notes": str}]. Quantities are per-delivery, not running totals, and
    accumulate onto PurchaseOrderLine.quantity_received.

    Over-delivery (cumulative received beyond approved) is refused unless
    confirm_overdelivery is set - never silently clamped and never
    silently accepted, because both hide a real discrepancy with the
    supplier.
    """
    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    if po.status not in (
        PurchaseOrder.Status.APPROVED,
        PurchaseOrder.Status.PARTIALLY_RECEIVED,
        # FULLY_RECEIVED is allowed on purpose. A supplier who turns up
        # with extra after the PO was already satisfied is a real thing,
        # and refusing outright would leave stock that physically exists
        # unrecordable. It is not a free pass: everything is already at
        # or past its approved quantity by definition, so any such
        # receipt trips the over-delivery guard below and needs
        # confirm_overdelivery. CLOSED stays blocked - closing is a
        # deliberate "we are done with this one" decision.
        PurchaseOrder.Status.FULLY_RECEIVED,
    ):
        raise ValueError(f"Cannot receive against a purchase order in {po.status} status.")

    lines = {line.id: line for line in po.lines.select_related("ingredient").select_for_update()}
    if not items:
        raise ValueError("A goods receipt needs at least one line.")

    over = []
    for entry in items:
        line = lines.get(entry["line"].id)
        if line is None:
            raise ValueError("That line does not belong to this purchase order.")
        qty = entry["received_quantity"]
        if qty <= 0:
            raise ValueError("A received quantity must be greater than zero.")
        limit = line.approved_quantity if line.approved_quantity is not None else line.quantity_ordered
        if line.quantity_received + qty > limit:
            over.append(
                f"{line.ingredient.name}: {line.quantity_received + qty} received vs {limit} approved"
            )
    if over and not confirm_overdelivery:
        raise OverDeliveryError(
            "More was received than approved for: " + "; ".join(over)
            + ". Re-send with confirm_overdelivery true to accept it anyway."
        )

    receipt = GoodsReceipt.objects.create(purchase_order=po, received_by=received_by, notes=notes or "")
    for entry in items:
        line = lines[entry["line"].id]
        qty = entry["received_quantity"]
        GoodsReceiptLine.objects.create(
            goods_receipt=receipt, purchase_order_line=line,
            received_quantity=qty, notes=entry.get("notes", "") or "",
        )
        add_stock(
            line.ingredient_id, qty, unit_cost=line.unit_cost, recorded_by=received_by,
            adjustment_reason=StockMovement.AdjustmentReason.GOODS_RECEIPT,
        )
        line.quantity_received = line.quantity_received + qty
        line.save(update_fields=["quantity_received"])

    po.status = _settle_receipt_status(po)
    po.save(update_fields=["status"])
    return receipt


def _settle_receipt_status(po):
    """FULLY_RECEIVED once every line has its approved quantity in, else
    PARTIALLY_RECEIVED. A line approved at 0 counts as satisfied - there
    was never anything to deliver."""
    for line in po.lines.all():
        expected = line.approved_quantity if line.approved_quantity is not None else line.quantity_ordered
        if line.quantity_received < expected:
            return PurchaseOrder.Status.PARTIALLY_RECEIVED
    return PurchaseOrder.Status.FULLY_RECEIVED


@transaction.atomic
def close_purchase_order(po_id, reason, closed_by=None):
    """Give up on the outstanding balance of a short-shipped PO."""
    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    if po.status != PurchaseOrder.Status.PARTIALLY_RECEIVED:
        raise ValueError(
            f"Only a partially received purchase order can be closed - this one is {po.status}."
        )
    if not (reason or "").strip():
        raise ValueError("A reason is required to close a short-shipped purchase order.")
    po.status = PurchaseOrder.Status.CLOSED
    po.closed_reason = reason.strip()
    po.save(update_fields=["status", "closed_reason"])

    # 2026-09-22, per Karwin. Fires exactly once, here, on a successful
    # close - never on a partial or full receipt, which stay silent
    # (record_goods_receipt sends nothing at all, by design). The reason
    # goes in the body because the reason is the whole point: it is the
    # short-shipment explanation nobody sees otherwise.
    transaction.on_commit(lambda: notify_role(
        ["ADMIN"], tenant=po.restaurant, type="PURCHASE_ORDER_CLOSED",
        title=f"Purchase order closed — {po.supplier_name or 'no supplier set'}",
        body=po.closed_reason,
        data={"purchase_order_id": str(po.id)}, branch=po.branch,
    ))
    return po


def purchase_order_discrepancy(po):
    """Ordered vs approved vs received, per line - what to chase the
    supplier about."""
    lines = []
    for line in po.lines.select_related("ingredient"):
        approved = line.approved_quantity
        expected = approved if approved is not None else line.quantity_ordered
        # Quantities go out as decimal STRINGS, matching the PO line
        # serializer and every other quantity in this API. Returned from a
        # plain dict, a Decimal renders as a JSON float - so this endpoint
        # said 20.0 where the PO itself says "20.00" for the very same
        # value, and floats are the wrong carrier for quantities anyway.
        lines.append({
            "line_id": line.id,
            "ingredient_id": str(line.ingredient_id),
            "ingredient_name": line.ingredient.name,
            "unit": line.ingredient.unit,
            "quantity_ordered": str(line.quantity_ordered),
            "approved_quantity": str(approved) if approved is not None else None,
            "quantity_received": str(line.quantity_received),
            # Quantised, so the string does not depend on which side of
            # max() won - the zero literal would otherwise render "0"
            # while a real gap renders "5.00".
            "outstanding": str(max(expected - line.quantity_received, Decimal("0")).quantize(Decimal("0.01"))),
            "over_received": str(max(line.quantity_received - expected, Decimal("0")).quantize(Decimal("0.01"))),
        })
    return {
        "purchase_order_id": str(po.id),
        "status": po.status,
        "approval_note": po.approval_note,
        "closed_reason": po.closed_reason,
        "lines": lines,
        "fully_satisfied": all(Decimal(row["outstanding"]) == 0 for row in lines),
    }


def _usage_in_window(ingredient, start, end):
    total = StockMovement.objects.filter(
        ingredient=ingredient, movement_type=StockMovement.MovementType.USAGE,
        recorded_at__gte=start, recorded_at__lt=end,
    ).aggregate(total=dj_models.Sum("quantity"))["total"]
    return total or Decimal("0")


def compute_ingredient_stats(ingredient, window_days=7):
    """Pure numeric analysis, no AI involved — compares this window's usage
    rate against the prior window of equal length to detect a trend, and
    projects days-until-stockout from the current window's daily rate.
    Used both to decide which ingredients are worth an AI insight and as
    the factual grounding handed to Groq so it can't invent numbers.
    """
    now = timezone.now()
    window_start = now - timedelta(days=window_days)
    prior_start = window_start - timedelta(days=window_days)

    recent_usage = _usage_in_window(ingredient, window_start, now)
    prior_usage = _usage_in_window(ingredient, prior_start, window_start)

    daily_rate = recent_usage / window_days if recent_usage else Decimal("0")
    prior_daily_rate = prior_usage / window_days if prior_usage else Decimal("0")

    trend_pct = None
    if prior_daily_rate > 0:
        trend_pct = round(float((daily_rate - prior_daily_rate) / prior_daily_rate) * 100, 1)

    # 2026-09-21: floored at 0. Stock can now be negative, and a negative
    # "days until stockout" is meaningless - already out is zero days away,
    # not minus three.
    days_until_stockout = (
        max(float(ingredient.current_stock / daily_rate), 0.0) if daily_rate > 0 else None
    )

    last_restock = (
        StockMovement.objects.filter(ingredient=ingredient, movement_type=StockMovement.MovementType.RESTOCK)
        .order_by("-recorded_at").first()
    )

    return {
        "ingredient": ingredient,
        "daily_usage_rate": daily_rate,
        "prior_daily_usage_rate": prior_daily_rate,
        "trend_pct": trend_pct,
        "days_until_stockout": days_until_stockout,
        "last_restocked_at": last_restock.recorded_at if last_restock else None,
    }


def _worth_flagging(ingredient, stats):
    """Which ingredients get an AI insight this run — anything already
    low/critical, trending sharply upward in usage even while still
    healthy (an early warning before it becomes low), or projected to run
    out within 3 days regardless of its current minimum_stock_level."""
    if ingredient.stock_status in ("critical", "low"):
        return True
    if stats["trend_pct"] is not None and stats["trend_pct"] >= 25:
        return True
    if stats["days_until_stockout"] is not None and stats["days_until_stockout"] <= 3:
        return True
    return False


AI_INSIGHTS_SYSTEM_PROMPT = (
    "You are the inventory analyst for a restaurant management app. Given "
    "structured stock-movement facts for a list of ingredients, return ONLY "
    "a JSON object of the shape {\"insights\": [...]}. Each item must have: "
    "ingredient_id (string, copied exactly from the matching input item), "
    "severity (one of CRITICAL, ALERT, TIP), headline (one short sentence a "
    "manager reads at a glance, citing the actual numbers given), "
    "reason_breakdown (1-2 sentences explaining why this was flagged, "
    "referencing the specific facts provided), recommended_action (one "
    "short actionable sentence, e.g. a suggested restock quantity). Never "
    "invent numbers not present in the input. Use CRITICAL only when "
    "stock_status is \"critical\" or estimated_days_until_stockout <= 1, "
    "ALERT for low stock or a fast-rising trend, TIP for everything else."
)


@transaction.atomic
def generate_ai_insights(restaurant, branch=None):
    """Manager Home / Stock screens' 'AI Insights' feed. Flags ingredients
    worth surfacing via pure stock-movement math (no AI needed for that
    part), then hands their facts to Groq in a single batched call to
    phrase the headline/reasoning/recommendation — one call regardless of
    how many ingredients are flagged, not one per ingredient.
    """
    from core.ai_client import generate_json

    qs = Ingredient.objects.filter(restaurant=restaurant, is_active=True)
    if branch is not None:
        # Strict since 2026-09-21, per Karwin — a branch-less ingredient no
        # longer gets flagged into every branch's insight feed at once.
        qs = qs.filter(branch=branch)

    flagged = [stats for stats in (compute_ingredient_stats(i) for i in qs) if _worth_flagging(stats["ingredient"], stats)]
    if not flagged:
        return []

    facts = [
        {
            "ingredient_id": str(s["ingredient"].id),
            "ingredient_name": s["ingredient"].name,
            "unit": s["ingredient"].unit,
            "current_stock": float(s["ingredient"].current_stock),
            "minimum_stock_level": float(s["ingredient"].minimum_stock_level),
            "stock_status": s["ingredient"].stock_status,
            "daily_usage_rate": float(s["daily_usage_rate"]),
            "usage_trend_pct_vs_prior_week": s["trend_pct"],
            "estimated_days_until_stockout": s["days_until_stockout"],
            "last_restocked_at": s["last_restocked_at"].isoformat() if s["last_restocked_at"] else None,
        }
        for s in flagged
    ]

    result = generate_json(AI_INSIGHTS_SYSTEM_PROMPT, json.dumps({"ingredients": facts}))
    raw_insights = result.get("insights", []) if isinstance(result, dict) else []

    by_id = {str(s["ingredient"].id): s["ingredient"] for s in flagged}
    created = []
    for item in raw_insights:
        ingredient = by_id.get(item.get("ingredient_id"))
        if ingredient is None:
            continue
        severity = item.get("severity")
        if severity not in AIInsight.Severity.values:
            severity = AIInsight.Severity.TIP
        created.append(
            AIInsight.objects.create(
                restaurant=restaurant, branch=branch, ingredient=ingredient, severity=severity,
                headline=item.get("headline", "")[:255],
                reason_breakdown=item.get("reason_breakdown", ""),
                recommended_action=item.get("recommended_action", "")[:255],
            )
        )
    return created
