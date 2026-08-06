import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.text import slugify


class Restaurant(models.Model):
    """A tenant on the DineOS platform — one row per client restaurant."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        TRIAL = "TRIAL", "Trial"
        SUSPENDED = "SUSPENDED", "Suspended"

    class PlanTier(models.TextChoices):
        STARTER = "STARTER", "Starter"
        GROWTH = "GROWTH", "Growth"
        ENTERPRISE = "ENTERPRISE", "Enterprise"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("5.00"))
    service_charge_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))

    # Org Identity — set by the Super Admin at onboarding (Create
    # Organization screen); contact_email is who the first Admin invite
    # goes to.
    contact_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    billing_email = models.EmailField(blank=True)
    primary_color = models.CharField(max_length=7, blank=True, default="#FF6B35")

    # Plan — an internal label only (no real payment processing). Picking
    # a plan pre-fills max_branches + the flags below from PLAN_PRESETS;
    # every value stays individually overridable afterward on this row.
    plan_tier = models.CharField(max_length=16, choices=PlanTier.choices, default=PlanTier.STARTER)
    max_branches = models.PositiveIntegerField(null=True, blank=True)  # None = unlimited (Enterprise)
    default_manager_spending_limit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

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


class Branch(models.Model):
    """A physical location belonging to a Restaurant tenant. Staff are
    assigned to a branch to indicate which location they work at."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, blank=True)
    address = models.CharField(max_length=500, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    photo_url = models.URLField(blank=True)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="managed_branches"
    )
    table_count = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "branches"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["restaurant", "name"], name="one_branch_name_per_restaurant"),
            models.UniqueConstraint(fields=["restaurant", "slug"], name="one_branch_slug_per_restaurant"),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.restaurant.slug})"
