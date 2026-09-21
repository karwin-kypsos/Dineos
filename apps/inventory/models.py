import uuid

from django.conf import settings
from django.db import models


class Ingredient(models.Model):
    class Unit(models.TextChoices):
        KG = "KG", "Kilograms"
        G = "G", "Grams"
        L = "L", "Liters"
        ML = "ML", "Milliliters"
        PCS = "PCS", "Pieces"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="ingredients")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="ingredients"
    )
    name = models.CharField(max_length=255)
    unit = models.CharField(max_length=4, choices=Unit.choices)
    current_stock = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    minimum_stock_level = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    supplier_name = models.CharField(max_length=255, blank=True)
    supplier_phone = models.CharField(max_length=20, blank=True)
    supplier_notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ingredients"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"], condition=models.Q(branch__isnull=True),
                name="one_ingredient_name_per_restaurant_legacy",
            ),
            models.UniqueConstraint(
                fields=["branch", "name"], condition=models.Q(branch__isnull=False),
                name="one_ingredient_name_per_branch",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.current_stock} {self.unit})"

    @property
    def is_low_stock(self):
        return self.current_stock <= self.minimum_stock_level

    @property
    def stock_status(self):
        """Three-tier classification for the frontend's stock filter/badge.
        critical: out of stock (<= 0) — needs an urgent reorder.
        low: below the reorder point but some is still on hand.
        healthy: above the reorder point.
        """
        if self.current_stock <= 0:
            return "critical"
        if self.current_stock <= self.minimum_stock_level:
            return "low"
        return "healthy"


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        RESTOCK = "RESTOCK", "Restock"
        WASTAGE = "WASTAGE", "Wastage"
        USAGE = "USAGE", "Usage"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"

    class WastageReason(models.TextChoices):
        SPOILED = "SPOILED", "Spoiled"
        OVER_PREPPED = "OVER_PREPPED", "Over-prepped"
        RETURNED = "RETURNED", "Returned"
        OTHER = "OTHER", "Other"

    # 2026-09-21, per the spec: a manual stock adjustment must now say
    # why, so a hand-typed correction is never mistaken for a real
    # delivery. WASTAGE is in the list as specified even though the
    # wastage endpoint is the normal way to record it - I flagged the
    # overlap and it was kept, so a manual downward correction entered
    # here can be labelled honestly instead of forced into another value.
    # GOODS_RECEIPT is set automatically by record_goods_receipt, never
    # by a caller, so PO-driven restocks stay distinguishable from
    # hand-entered ones in the movement history.
    class AdjustmentReason(models.TextChoices):
        STOCK_COUNT_CORRECTION = "STOCK_COUNT_CORRECTION", "Stock count correction"
        WASTAGE = "WASTAGE", "Wastage"
        OPENING_STOCK = "OPENING_STOCK", "Opening stock"
        GOODS_RECEIPT = "GOODS_RECEIPT", "Goods receipt"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name="movements")
    movement_type = models.CharField(max_length=16, choices=MovementType.choices)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    wastage_reason = models.CharField(max_length=16, choices=WastageReason.choices, blank=True)
    adjustment_reason = models.CharField(max_length=32, choices=AdjustmentReason.choices, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    unit_cost_at_time = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stock_movements"
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"{self.movement_type} {self.quantity} {self.ingredient.unit} of {self.ingredient.name}"


class PurchaseOrder(models.Model):
    # 2026-09-21, rewritten to the goods-receipt spec. The old enum was
    # PENDING / APPROVED / REJECTED / ORDERED / RECEIVED. Two deliberate
    # changes, both confirmed after I flagged them:
    #   - ORDERED is gone. It had its own endpoint and live rows, but the
    #     spec goes approve -> receive with no separate "placed with the
    #     supplier" step. Migration maps existing ORDERED rows to APPROVED
    #     (approved, nothing received yet), which is what they mean.
    #   - DRAFT is new. Nothing creates one yet; it exists so the app can
    #     save an unsent request later without another migration.
    # Values stay UPPER_SNAKE to match every other enum in this API (order
    # status, payment method, wastage reason). Only the state machine
    # follows the spec, not its lowercase spelling.
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", "Partially received"
        FULLY_RECEIVED = "FULLY_RECEIVED", "Fully received"
        CLOSED = "CLOSED", "Closed"

    class Reason(models.TextChoices):
        AI_ALERT = "AI_ALERT", "AI alert"
        SUPPLIER_CALLED = "SUPPLIER_CALLED", "Supplier called"
        NOTICED = "NOTICED", "I noticed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="purchase_orders")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="purchase_orders"
    )
    # max_length 32: PARTIALLY_RECEIVED is 18 characters, the old 16 truncates.
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING_APPROVAL)
    reason = models.CharField(max_length=20, choices=Reason.choices, blank=True)
    # "Emergency purchase (already bought)?" toggle on New Restock Request —
    # the ingredient was already physically bought on the spot, not
    # requested for later approval. create_purchase_order() skips straight
    # to RECEIVED and restocks immediately when this is set, since there's
    # nothing left to approve/order for something that already happened.
    is_emergency = models.BooleanField(default=False)
    supplier_name = models.CharField(max_length=255, blank=True)
    supplier_notes = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="requested_purchase_orders"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_purchase_orders"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    # Why an approved quantity differs from what was requested.
    approval_note = models.TextField(blank=True)
    # Why a short-shipped PO was closed without the rest ever arriving.
    closed_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "purchase_orders"
        ordering = ["-created_at"]

    def __str__(self):
        return f"PO {self.id} ({self.status})"


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="purchase_order_lines")
    # quantity_ordered is the spec's requested_quantity and
    # quantity_received its received_quantity - kept under the existing
    # names rather than renamed, since the spec allowed either and a
    # rename would break every client reading them today.
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2)
    # Null until approved. May be less than ordered - that is the point of
    # approving per line, and the gap is what approval_note explains.
    approved_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Cumulative across every goods receipt, append-only. Never overwritten
    # and never decreased - corrections go through the manual adjustment
    # path so they stay visible as corrections.
    quantity_received = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "purchase_order_lines"
        # Stable line order on a purchase order, same reasoning as
        # OrderItem - a PO's lines should not reshuffle between views.
        ordering = ["id"]

    def __str__(self):
        return f"{self.quantity_ordered} {self.ingredient.unit} of {self.ingredient.name}"


class GoodsReceipt(models.Model):
    """One delivery against a purchase order (2026-09-21, per the spec).

    A PO can have many of these - a supplier who short-ships on Monday and
    sends the rest on Thursday produces two receipts against the same PO,
    and the line's quantity_received accumulates across both.

    This is the ONLY thing in the system allowed to increase stock from a
    purchase order. Raising a PO does not move stock and neither does
    approving one; see apps.inventory.services.record_goods_receipt.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="goods_receipts")
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="goods_receipts"
    )
    received_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "goods_receipts"
        ordering = ["received_at", "id"]

    def __str__(self):
        return f"Receipt for PO {self.purchase_order_id} at {self.received_at}"


class GoodsReceiptLine(models.Model):
    """What actually turned up for one PO line in one delivery.

    received_quantity here is THIS delivery only - the running total lives
    on PurchaseOrderLine.quantity_received, which this updates.
    """

    goods_receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.CASCADE, related_name="receipt_lines"
    )
    received_quantity = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "goods_receipt_items"
        ordering = ["id"]

    def __str__(self):
        return f"{self.received_quantity} received against line {self.purchase_order_line_id}"


class RecipeItem(models.Model):
    """How much of an ingredient one serving of a menu item uses — 'Dishes using
    this ingredient' on the ingredient detail screen. Phase-1 reference data
    only; automatic stock deduction on order placement is not wired up yet."""

    menu_item = models.ForeignKey("menu.MenuItem", on_delete=models.CASCADE, related_name="recipe_items")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name="recipe_items")
    quantity_per_serving = models.DecimalField(max_digits=10, decimal_places=3)

    class Meta:
        db_table = "recipe_items"
        # Same paginated-but-unordered bug as KDSDevice (see kitchen.models)
        # - this one just never warned, because the warning only fires when
        # the paginator sees the queryset directly.
        ordering = ["menu_item_id", "id"]
        constraints = [
            models.UniqueConstraint(fields=["menu_item", "ingredient"], name="one_recipe_line_per_item_ingredient")
        ]

    def __str__(self):
        return f"{self.menu_item.name} uses {self.quantity_per_serving} {self.ingredient.unit} {self.ingredient.name}"


class AIInsight(models.Model):
    """A Groq-generated stock observation — 'AI Insights' / 'AI Alert' /
    'AI Daily Insight' feed on the Manager Home / Stock screens. Persisted
    (not computed fresh per request) so the feed is cheap to read and each
    insight can be dismissed independently of the next generation run."""

    class Severity(models.TextChoices):
        CRITICAL = "CRITICAL", "Critical"
        ALERT = "ALERT", "Alert"
        TIP = "TIP", "Tip"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="ai_insights")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="ai_insights"
    )
    ingredient = models.ForeignKey(
        Ingredient, on_delete=models.CASCADE, null=True, blank=True, related_name="ai_insights"
    )
    severity = models.CharField(max_length=8, choices=Severity.choices)
    headline = models.CharField(max_length=255)
    reason_breakdown = models.TextField(blank=True)
    recommended_action = models.CharField(max_length=255, blank=True)
    is_dismissed = models.BooleanField(default=False)
    generated_at = models.DateTimeField(auto_now_add=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ai_insights"
        ordering = ["-generated_at"]

    def __str__(self):
        return f"[{self.severity}] {self.headline}"
