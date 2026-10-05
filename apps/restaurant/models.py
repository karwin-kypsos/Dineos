import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
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
    # Only meaningful while status=TRIAL — set automatically (14 days out)
    # the moment a tenant becomes TRIAL, either at creation or via the
    # Organization Detail status toggle. Null for tenants that have never
    # been on trial. Drives the Super Admin dashboard's "needing attention"
    # list once a trial runs out (see apps.platform.views.DashboardView).
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("5.00"))
    service_charge_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))

    # Org Identity — set by the Super Admin at onboarding (Create
    # Organization screen); contact_email is who the first Admin invite
    # goes to.
    contact_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    billing_email = models.EmailField(blank=True)
    # The restaurant's GSTIN (2026-09-29, Admin Self-Registration) - its
    # tax registration number, not a rate; gst_percentage above is the rate.
    gst_number = models.CharField(max_length=15, blank=True)
    # 2026-10-01, per Shereena: on the registration form beside the GSTIN.
    # The business's PAN, and its registration number - free text, since it
    # can be a CIN, an LLPIN, an Udyam number or a shop licence number.
    pan_number = models.CharField(max_length=10, blank=True)
    business_registration_number = models.CharField(max_length=50, blank=True)
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
    # Realtime updates stopped being a flag on 2026-10-01, per Karwin: every
    # restaurant on every plan gets them, so the column is gone. Kept as a
    # constant so /v1/auth/me/, Super Admin and the QR response still say
    # realtime_enabled: true to the apps that read it.
    realtime_enabled = True
    # 2026-09-30, per Shereena: customers ordering for themselves from the
    # table QR code, and whether "Server" staff accounts may be added (some
    # restaurants run fully self-order with no order-taking staff).
    customer_ordering_enabled = models.BooleanField(default=True)
    server_staff_enabled = models.BooleanField(default=True)

    # Razorpay Route linked account id (2026-09-09) — this restaurant's own
    # Razorpay sub-account, set once they've completed Razorpay's own hosted
    # onboarding/KYC outside this app. Blank means "not onboarded to
    # Razorpay yet", which apps.payments treats as Razorpay collection being
    # off for this restaurant — the existing manual CASH/CARD/UPI flow in
    # apps.billing is unaffected either way.
    razorpay_account_id = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "restaurants"

    def __str__(self):
        return self.name


class RestaurantRegistration(models.Model):
    """A restaurant signing itself up (2026-09-29, Admin Self-Registration):
    the details and plan from step 2, waiting for step 3 to set the Admin's
    login.

    The Restaurant row is only created at signup, so a registration that is
    abandoned halfway leaves nothing in the Super Admin's organization list.
    The id is the registration_id the client carries between the two steps:
    a random UUID, never the Restaurant's sequential id - step 3 needs no
    login, so anyone who could guess the id could set the new restaurant's
    admin password.
    """

    class Status(models.TextChoices):
        PENDING_SIGNUP = "PENDING_SIGNUP", "Waiting for signup"
        COMPLETED = "COMPLETED", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255)
    contact_phone = models.CharField(max_length=32)
    contact_email = models.EmailField()
    billing_email = models.EmailField(blank=True)
    gst_number = models.CharField(max_length=15, blank=True)
    pan_number = models.CharField(max_length=10, blank=True)
    business_registration_number = models.CharField(max_length=50, blank=True)
    service_charge_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    plan_tier = models.CharField(max_length=16, choices=Restaurant.PlanTier.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING_SIGNUP)
    restaurant = models.OneToOneField(
        Restaurant, on_delete=models.SET_NULL, null=True, blank=True, related_name="registration"
    )
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "restaurant_registrations"

    def __str__(self):
        return f"{self.restaurant_name} ({self.status})"


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
        # Lower(): case-insensitive A-Z (2026-10-05, per Karwin). Neon sorts
        # text byte-wise (C.UTF-8), so "Zeta" came before "alpha"; the old
        # Render database ignored case. Done here so it holds on any database.
        ordering = [Lower("name"), "id"]
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
