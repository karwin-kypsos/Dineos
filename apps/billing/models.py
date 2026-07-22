import uuid

from django.conf import settings
from django.db import models


class Bill(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        CARD = "CARD", "Card"
        UPI = "UPI", "UPI"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField("tables.TableSession", on_delete=models.CASCADE, related_name="bill")
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    service_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=8, choices=PaymentMethod.choices)
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="processed_bills")
    paid_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bills"
        ordering = ["-paid_at"]

    def __str__(self):
        return f"Bill {self.id} — {self.total_amount}"
