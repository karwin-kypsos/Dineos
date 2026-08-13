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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name="movements")
    movement_type = models.CharField(max_length=16, choices=MovementType.choices)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    wastage_reason = models.CharField(max_length=16, choices=WastageReason.choices, blank=True)
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
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        ORDERED = "ORDERED", "Ordered"
        RECEIVED = "RECEIVED", "Received"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="purchase_orders")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="purchase_orders"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    supplier_name = models.CharField(max_length=255, blank=True)
    supplier_notes = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="requested_purchase_orders"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_purchase_orders"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "purchase_orders"
        ordering = ["-created_at"]

    def __str__(self):
        return f"PO {self.id} ({self.status})"


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="purchase_order_lines")
    quantity_ordered = models.DecimalField(max_digits=10, decimal_places=2)
    quantity_received = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "purchase_order_lines"

    def __str__(self):
        return f"{self.quantity_ordered} {self.ingredient.unit} of {self.ingredient.name}"


class RecipeItem(models.Model):
    """How much of an ingredient one serving of a menu item uses — 'Dishes using
    this ingredient' on the ingredient detail screen. Phase-1 reference data
    only; automatic stock deduction on order placement is not wired up yet."""

    menu_item = models.ForeignKey("menu.MenuItem", on_delete=models.CASCADE, related_name="recipe_items")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name="recipe_items")
    quantity_per_serving = models.DecimalField(max_digits=10, decimal_places=3)

    class Meta:
        db_table = "recipe_items"
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
