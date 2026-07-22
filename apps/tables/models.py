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
    table_number = models.CharField(max_length=32, unique=True)
    capacity = models.PositiveIntegerField(default=4)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AVAILABLE)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tables"
        ordering = ["table_number"]

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
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="closed_sessions"
    )
    close_reason = models.CharField(max_length=20, choices=CloseReason.choices, blank=True)

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
