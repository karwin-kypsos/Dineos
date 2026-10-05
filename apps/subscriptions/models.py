"""A restaurant paying DineOS for its plan (2026-10-05, per Karwin), via
Razorpay Subscriptions. Distinct from apps.payments, which is a diner paying
a restaurant."""
import uuid

from django.conf import settings
from django.db import models

# Higher number = bigger plan; decides upgrade vs downgrade.
TIER_RANK = {"STARTER": 1, "GROWTH": 2, "ENTERPRISE": 3}


class RazorpayPlan(models.Model):
    """Our tier + billing cycle + price -> the Razorpay Plan holding it.
    Razorpay plans can't be edited, so changing a price creates a new row
    (and a new Razorpay plan) the first time someone checks out at it."""

    plan_tier = models.CharField(max_length=16)
    billing_cycle = models.CharField(max_length=8, default="MONTHLY")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    razorpay_plan_id = models.CharField(max_length=64, unique=True)
    # Which Razorpay key made it: a test-mode plan doesn't exist for the live
    # keys, so switching keys must not reuse it.
    razorpay_key_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscription_razorpay_plans"
        constraints = [
            models.UniqueConstraint(
                fields=["plan_tier", "billing_cycle", "amount", "razorpay_key_id"], name="one_razorpay_plan_per_price_and_key",
            ),
        ]


class Subscription(models.Model):
    """One Razorpay subscription. A plan change makes a new one that
    `replaces` the old; the old is cancelled at the end of its paid period.

    Statuses follow Razorpay's own: CREATED (checkout not finished yet),
    AUTHENTICATED (mandate approved, first charge on start_at), ACTIVE,
    PENDING (a renewal failed, Razorpay is retrying), HALTED (retries ran
    out), CANCELLED, COMPLETED.
    """

    class Status(models.TextChoices):
        CREATED = "CREATED", "Waiting for checkout"
        AUTHENTICATED = "AUTHENTICATED", "Authorised, billing starts later"
        ACTIVE = "ACTIVE", "Active"
        PENDING = "PENDING", "Renewal failed, retrying"
        HALTED = "HALTED", "Renewals failed"
        CANCELLED = "CANCELLED", "Cancelled"
        COMPLETED = "COMPLETED", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="subscriptions")
    plan_tier = models.CharField(max_length=16)
    billing_cycle = models.CharField(max_length=8, default="MONTHLY")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    razorpay_plan_id = models.CharField(max_length=64)
    razorpay_subscription_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CREATED)
    # First charge. Null means "at checkout"; otherwise the end of a trial or
    # of the period already paid for on the plan this one replaces.
    start_at = models.DateTimeField(null=True, blank=True)
    current_start = models.DateTimeField(null=True, blank=True)
    current_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    replaces = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replaced_by")
    # When this subscription's tier was put on the restaurant (an upgrade at
    # authorisation, a downgrade or a first plan once billing starts).
    plan_applied_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "subscriptions"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.plan_tier} {self.status} ({self.razorpay_subscription_id})"


class SubscriptionPayment(models.Model):
    """One charge on a subscription - the "billing history" screen."""

    class Status(models.TextChoices):
        PAID = "PAID", "Paid"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="payments")
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="subscription_payments")
    razorpay_payment_id = models.CharField(max_length=64, unique=True)
    razorpay_invoice_id = models.CharField(max_length=64, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=8, choices=Status.choices)
    method = models.CharField(max_length=20, blank=True)
    plan_tier = models.CharField(max_length=16)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscription_payments"
        ordering = ["-created_at"]
