import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Table(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        OCCUPIED = "OCCUPIED", "Occupied"
        RESERVED = "RESERVED", "Reserved"
        NEEDS_CLEANING = "NEEDS_CLEANING", "Needs Cleaning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="tables")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="tables"
    )
    table_number = models.CharField(max_length=32)
    capacity = models.PositiveIntegerField(default=4)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AVAILABLE)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tables"
        ordering = ["table_number"]
        constraints = [
            # Legacy (pre-branch) tables: unique per restaurant.
            models.UniqueConstraint(
                fields=["restaurant", "table_number"],
                condition=models.Q(branch__isnull=True),
                name="one_table_number_per_restaurant_legacy",
            ),
            # Once a table belongs to a branch, uniqueness is per-branch —
            # two branches of the same restaurant can both have a "Table 5".
            models.UniqueConstraint(
                fields=["branch", "table_number"],
                condition=models.Q(branch__isnull=False),
                name="one_table_number_per_branch",
            ),
        ]

    def __str__(self):
        return f"Table {self.table_number}"


class TableSession(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        BILL_REQUESTED = "BILL_REQUESTED", "Bill Requested"
        CLOSED = "CLOSED", "Closed"

    class CloseReason(models.TextChoices):
        PAID = "PAID", "Paid"
        MANAGER_OVERRIDE = "MANAGER_OVERRIDE", "Manager Override"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    table = models.ForeignKey(Table, on_delete=models.CASCADE, related_name="sessions")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    opened_at = models.DateTimeField(auto_now_add=True)
    # Set once, in services.request_bill (2026-08-25) — the "Bill requested
    # by customer" entry on the Bill Detail Transaction Timeline needs its
    # own timestamp; status alone only says BILL_REQUESTED, not when.
    bill_requested_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="closed_sessions"
    )
    close_reason = models.CharField(max_length=20, choices=CloseReason.choices, blank=True)
    # Set once, on the session's first order (see apps.tables.services.assign_next_server) —
    # round-robin among the branch's active Servers. Never reassigned mid-session.
    assigned_server = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_sessions"
    )

    class Meta:
        db_table = "table_sessions"
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["table"],
                condition=Q(status__in=["ACTIVE", "BILL_REQUESTED"]),
                name="one_open_session_per_table",
            )
        ]

    def __str__(self):
        return f"Session {self.id} @ Table {self.table.table_number} ({self.status})"
