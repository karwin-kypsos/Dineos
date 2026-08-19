import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class CashierShift(models.Model):
    """One cashier's work period — 'Shift ID #SCH-...' / 'Cash
    Reconciliation' in the Cashier Home + Close Shift screens. `Bill.
    processed_by` + `Bill.paid_at` are the single source of truth for what a
    cashier collected during their shift; this model never duplicates those
    totals, it only stores the physical cash count entered at close time
    (everything else is computed on demand in `services.py`).
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="cashier_shifts")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="cashier_shifts"
    )
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="cashier_shifts")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    counted_cash = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discrepancy_acknowledged = models.BooleanField(default=False)
    # counted_cash - the system's cash total at close time, persisted so it
    # never has to be recomputed later — 0 when there was no mismatch.
    discrepancy_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    discrepancy_reason = models.TextField(blank=True)

    class Meta:
        db_table = "cashier_shifts"
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(fields=["cashier"], condition=models.Q(status="OPEN"), name="one_open_shift_per_cashier")
        ]

    def __str__(self):
        return f"Shift {self.id} — {self.cashier} ({self.status})"


class Bill(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "CASH", "Cash"
        CARD = "CARD", "Card"
        UPI = "UPI", "UPI"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Dine-in: session set, order null. Takeaway: order set, session null.
    session = models.OneToOneField(
        "tables.TableSession", on_delete=models.CASCADE, null=True, blank=True, related_name="bill"
    )
    order = models.OneToOneField(
        "orders.Order", on_delete=models.CASCADE, null=True, blank=True, related_name="takeaway_bill"
    )
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="bills"
    )
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
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(session__isnull=False, order__isnull=True)
                    | models.Q(session__isnull=True, order__isnull=False)
                ),
                name="bill_has_exactly_one_of_session_or_order",
            ),
        ]

    def __str__(self):
        return f"Bill {self.id} — {self.total_amount}"
