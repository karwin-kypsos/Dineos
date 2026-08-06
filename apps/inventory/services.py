from django.db import transaction

from .models import Ingredient, PurchaseOrder, PurchaseOrderLine, StockMovement


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
