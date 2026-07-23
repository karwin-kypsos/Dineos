from decimal import Decimal

from django.db import models


class Restaurant(models.Model):
    """A tenant on the DineOS platform — one row per client restaurant."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("5.00"))
    service_charge_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))

    # Per-tenant add-on flags — controlled by the platform Super Admin,
    # not by deployment config. Every deployment ships with all four on;
    # a Super Admin dials individual clients down for cheaper tiers.
    notifications_enabled = models.BooleanField(default=True)
    kitchen_enabled = models.BooleanField(default=True)
    billing_enabled = models.BooleanField(default=True)
    realtime_enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "restaurants"

    def __str__(self):
        return self.name
