from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Type(models.TextChoices):
        ORDER_READY = "ORDER_READY", "Order Ready"
        BILL_REQUESTED = "BILL_REQUESTED", "Bill Requested"
        PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED", "Payment Confirmed"

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
