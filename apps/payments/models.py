import uuid

from django.conf import settings
from django.db import models


class PaymentAttempt(models.Model):
    """One Razorpay Order per cashier-initiated CARD/UPI payment attempt.
    Mirrors apps.billing.models.Bill's session-xor-order shape (dine-in vs
    takeaway) but is deliberately a separate model, not a Bill field —
    a Bill only ever exists once payment is CONFIRMED (via the webhook
    calling the existing apps.billing.services.pay_bill/pay_takeaway_bill),
    so a customer who abandons checkout mid-payment leaves no Bill behind,
    same as today's behavior when a cashier never gets around to clicking
    Pay.
    """

    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        PAID = "PAID", "Paid"
        FAILED = "FAILED", "Failed"

    class PaymentMethod(models.TextChoices):
        CARD = "CARD", "Card"
        UPI = "UPI", "UPI"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "tables.TableSession", on_delete=models.CASCADE, null=True, blank=True, related_name="payment_attempts"
    )
    order = models.ForeignKey(
        "orders.Order", on_delete=models.CASCADE, null=True, blank=True, related_name="payment_attempts"
    )
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="payment_attempts")
    razorpay_order_id = models.CharField(max_length=64, unique=True)
    payment_method = models.CharField(max_length=8, choices=PaymentMethod.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.CREATED)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="payment_attempts"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "payment_attempts"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(session__isnull=False, order__isnull=True)
                    | models.Q(session__isnull=True, order__isnull=False)
                ),
                name="payment_attempt_has_exactly_one_of_session_or_order",
            ),
        ]

    def __str__(self):
        return f"PaymentAttempt {self.id} — {self.razorpay_order_id} ({self.status})"
