import uuid

from django.conf import settings
from django.db import models


class Order(models.Model):
    class Status(models.TextChoices):
        NEW = "NEW", "New"
        ACCEPTED = "ACCEPTED", "Accepted"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        COLLECTED = "COLLECTED", "Collected"
        SERVED = "SERVED", "Served"
        CANCELLED = "CANCELLED", "Cancelled"

    # Per-item kitchen status (see OrderItem.status below) only ever needs
    # the kitchen-prep subset of the whole-order lifecycle — COLLECTED/
    # SERVED/CANCELLED stay order-level-only concepts (collection and
    # serving happen once, for the whole order; cancellation is decided
    # order-wide too). Kept as a subset of Order.Status rather than a
    # separate TextChoices so the frontend only has one status vocabulary
    # to deal with across both order- and item-level fields.
    ITEM_STATUSES = (Status.NEW, Status.ACCEPTED, Status.PREPARING, Status.READY)

    class OrderType(models.TextChoices):
        DINE_IN = "DINE_IN", "Dine-in"
        TAKEAWAY = "TAKEAWAY", "Takeaway"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_type = models.CharField(max_length=16, choices=OrderType.choices, default=OrderType.DINE_IN)
    # Dine-in: both set. Takeaway: both null — see customer_name/phone instead.
    session = models.ForeignKey(
        "tables.TableSession", on_delete=models.CASCADE, null=True, blank=True, related_name="orders"
    )
    table = models.ForeignKey(
        "tables.Table", on_delete=models.CASCADE, null=True, blank=True, related_name="orders"
    )
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders"
    )
    # Takeaway-only "next round" support (2026-08-22): dine-in gets extra
    # rounds for free by reusing session_id across POST /v1/orders calls
    # (see services.place_order's round_number computation); takeaway has no
    # session to reuse, so a later round instead points parent_order at the
    # very first order placed for that customer. Null on the first round
    # (the "root") and on every dine-in order. See services.place_takeaway_order.
    parent_order = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="rounds"
    )
    customer_name = models.CharField(max_length=255, blank=True)
    customer_phone = models.CharField(max_length=32, blank=True)
    round_number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW)
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="placed_orders"
    )
    notes = models.CharField(max_length=255, blank=True)
    placed_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    preparing_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    served_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "orders"
        ordering = ["placed_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(order_type="DINE_IN", session__isnull=False, table__isnull=False)
                    | models.Q(order_type="TAKEAWAY", session__isnull=True, table__isnull=True)
                ),
                name="dine_in_has_table_takeaway_does_not",
            ),
        ]

    def __str__(self):
        if self.order_type == self.OrderType.TAKEAWAY:
            return f"Takeaway order — {self.customer_name or 'walk-in'}"
        return f"Order round {self.round_number} @ Table {self.table.table_number}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    menu_item = models.ForeignKey("menu.MenuItem", on_delete=models.PROTECT, related_name="order_items")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.CharField(max_length=255, blank=True)
    # Per-item kitchen status — lets "one item plated, another still
    # cooking" be represented independently of the whole-order status.
    # See PATCH /v1/orders/{order_id}/items/{item_id}/status/ and
    # apps.orders.services.advance_item_kitchen_status.
    status = models.CharField(
        max_length=16,
        choices=[choice for choice in Order.Status.choices if choice[0] in Order.ITEM_STATUSES],
        default=Order.Status.NEW,
    )

    class Meta:
        db_table = "order_items"

    @property
    def line_total(self):
        return self.unit_price * self.quantity

    def __str__(self):
        return f"{self.quantity} x {self.menu_item.name}"
