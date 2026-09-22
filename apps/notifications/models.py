from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Type(models.TextChoices):
        ORDER_READY = "ORDER_READY", "Order Ready"
        BILL_REQUESTED = "BILL_REQUESTED", "Bill Requested"
        PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED", "Payment Confirmed"
        LOW_STOCK = "LOW_STOCK", "Low Stock"
        # 2026-09-21, per Karwin: hitting zero used to produce NO alert at
        # all. The 14 Sep change suppressed critical to stop double-alerting,
        # but that left running out entirely - the most urgent case - silent.
        # Separate type rather than reusing LOW_STOCK so the app can style
        # and sort it differently.
        CRITICAL_STOCK = "CRITICAL_STOCK", "Critical Stock"
        STAFF_ADDED = "STAFF_ADDED", "Staff Added"
        PREP_LOGGED = "PREP_LOGGED", "Prep Logged"
        # 2026-09-10, per Shereena - the Purchase Order approval workflow
        # (Manager raises -> Admin approves/rejects -> Manager orders) had no
        # notifications at all on either side of it.
        PURCHASE_ORDER_RAISED = "PURCHASE_ORDER_RAISED", "Purchase Order Raised"
        PURCHASE_ORDER_APPROVED = "PURCHASE_ORDER_APPROVED", "Purchase Order Approved"
        PURCHASE_ORDER_REJECTED = "PURCHASE_ORDER_REJECTED", "Purchase Order Rejected"
        PURCHASE_ORDER_ORDERED = "PURCHASE_ORDER_ORDERED", "Purchase Order Ordered"
        # 2026-09-22, per Karwin: a short-shipped PO being written off is
        # the one PO transition worth interrupting someone for - the reason
        # typed at close time IS the value (discontinued item, supplier
        # cannot source the rest), and nobody sees it unless told.
        # Deliberately NOT fired for PARTIALLY_RECEIVED or FULLY_RECEIVED:
        # a delivery arriving is routine and would just be noise.
        PURCHASE_ORDER_CLOSED = "PURCHASE_ORDER_CLOSED", "Purchase Order Closed"

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications"
    )
    type = models.CharField(max_length=32, choices=Type.choices)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    order = models.ForeignKey("orders.Order", on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications")
    table = models.ForeignKey("tables.Table", on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications")
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notifications"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
