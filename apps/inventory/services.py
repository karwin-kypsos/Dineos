import json
from datetime import timedelta
from decimal import Decimal

from django.db import models as dj_models
from django.db import transaction
from django.utils import timezone

from .models import AIInsight, Ingredient, PurchaseOrder, PurchaseOrderLine, StockMovement


class InsufficientStockError(Exception):
    pass


@transaction.atomic
def add_stock(ingredient_id, quantity, unit_cost=None, recorded_by=None):
    ingredient = Ingredient.objects.select_for_update().get(id=ingredient_id)
    ingredient.current_stock += quantity
    if unit_cost is not None:
        ingredient.unit_cost = unit_cost
    ingredient.save(update_fields=["current_stock", "unit_cost"])
    return StockMovement.objects.create(
        ingredient=ingredient, movement_type=StockMovement.MovementType.RESTOCK,
        quantity=quantity, unit_cost_at_time=unit_cost or ingredient.unit_cost, recorded_by=recorded_by,
    )


@transaction.atomic
def record_wastage(ingredient_id, quantity, wastage_reason, reason="", recorded_by=None):
    ingredient = Ingredient.objects.select_for_update().get(id=ingredient_id)
    if quantity > ingredient.current_stock:
        raise InsufficientStockError(
            f"Cannot record {quantity} {ingredient.unit} of wastage — only {ingredient.current_stock} in stock."
        )
    ingredient.current_stock -= quantity
    ingredient.save(update_fields=["current_stock"])
    return StockMovement.objects.create(
        ingredient=ingredient, movement_type=StockMovement.MovementType.WASTAGE,
        quantity=quantity, wastage_reason=wastage_reason, reason=reason,
        unit_cost_at_time=ingredient.unit_cost, recorded_by=recorded_by,
    )


@transaction.atomic
def deduct_for_usage(ingredient_id, quantity, recorded_by=None):
    """Used by the Daily Prep Log recipe deduction. Unlike record_wastage,
    this deliberately does NOT block on insufficient stock: the Manager has
    already physically prepared the dish by the time this runs, so refusing
    to log it wouldn't undo that — it would just leave the prep log out of
    sync with reality. Going negative here is a legitimate signal ("stock
    count needs a recount"), not an error state."""
    ingredient = Ingredient.objects.select_for_update().get(id=ingredient_id)
    ingredient.current_stock -= quantity
    ingredient.save(update_fields=["current_stock"])
    return StockMovement.objects.create(
        ingredient=ingredient, movement_type=StockMovement.MovementType.USAGE,
        quantity=quantity, unit_cost_at_time=ingredient.unit_cost, recorded_by=recorded_by,
    )


@transaction.atomic
def create_purchase_order(restaurant, branch, lines, supplier_name="", supplier_notes="", requested_by=None):
    po = PurchaseOrder.objects.create(
        restaurant=restaurant, branch=branch, supplier_name=supplier_name,
        supplier_notes=supplier_notes, requested_by=requested_by,
    )
    for line in lines:
        PurchaseOrderLine.objects.create(
            purchase_order=po, ingredient=line["ingredient"],
            quantity_ordered=line["quantity_ordered"], unit_cost=line.get("unit_cost"),
        )
    return po


@transaction.atomic
def approve_purchase_order(po_id, approved_by):
    from django.utils import timezone

    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    if po.status != PurchaseOrder.Status.PENDING:
        raise ValueError(f"Cannot approve a purchase order in {po.status} status.")
    po.status = PurchaseOrder.Status.APPROVED
    po.approved_by = approved_by
    po.approved_at = timezone.now()
    po.save(update_fields=["status", "approved_by", "approved_at"])
    return po


@transaction.atomic
def reject_purchase_order(po_id, rejected_by):
    from django.utils import timezone

    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    if po.status != PurchaseOrder.Status.PENDING:
        raise ValueError(f"Cannot reject a purchase order in {po.status} status.")
    po.status = PurchaseOrder.Status.REJECTED
    po.approved_by = rejected_by
    po.approved_at = timezone.now()
    po.save(update_fields=["status", "approved_by", "approved_at"])
    return po


@transaction.atomic
def mark_purchase_order_ordered(po_id):
    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    if po.status != PurchaseOrder.Status.APPROVED:
        raise ValueError(f"Cannot mark a purchase order in {po.status} status as ordered.")
    po.status = PurchaseOrder.Status.ORDERED
    po.save(update_fields=["status"])
    return po


@transaction.atomic
def receive_purchase_order(po_id, recorded_by=None):
    """Marks every line fully received and restocks each ingredient in one
    atomic sweep — a partial/mixed-delivery receive can be added later by
    accepting per-line quantities instead, but every line is always
    all-or-nothing within this single transaction either way."""
    po = PurchaseOrder.objects.select_for_update().get(id=po_id)
    if po.status != PurchaseOrder.Status.ORDERED:
        raise ValueError(f"Cannot receive a purchase order in {po.status} status.")

    for line in po.lines.select_related("ingredient").select_for_update():
        outstanding = line.quantity_ordered - line.quantity_received
        if outstanding > 0:
            add_stock(line.ingredient_id, outstanding, unit_cost=line.unit_cost, recorded_by=recorded_by)
            line.quantity_received = line.quantity_ordered
            line.save(update_fields=["quantity_received"])

    po.status = PurchaseOrder.Status.RECEIVED
    po.save(update_fields=["status"])
    return po


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

    days_until_stockout = float(ingredient.current_stock / daily_rate) if daily_rate > 0 else None

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
        qs = qs.filter(dj_models.Q(branch=branch) | dj_models.Q(branch__isnull=True))

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
