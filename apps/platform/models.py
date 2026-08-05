import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class PlatformAdminManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        admin = self.model(email=email, **extra_fields)
        admin.set_password(password)
        admin.save(using=self._db)
        return admin

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra_fields)


class PlatformAdmin(AbstractBaseUser, PermissionsMixin):
    """Platform operator — manages client restaurants and their feature
    flags. Deliberately NOT the same auth realm as apps.authentication.User:
    a platform token never carries a restaurant_id claim, so it structurally
    cannot be mistaken for (or escalate into) a restaurant staff session.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    groups = models.ManyToManyField("auth.Group", related_name="platform_admins", blank=True)
    user_permissions = models.ManyToManyField("auth.Permission", related_name="platform_admins", blank=True)

    objects = PlatformAdminManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "platform_admins"

    def __str__(self):
        return self.email


class PlatformActivityLog(models.Model):
    """Audit trail for actions taken by platform team members — powers the
    Super Admin app's Activity Log screen. Written by the views that
    perform the action (tenant create/update, team member added, etc.)
    rather than via signals, so the description text stays human-readable
    and action-specific instead of generic field-diff dumps.
    """

    ACTION_CHOICES = [
        ("TENANT_CREATED", "Tenant created"),
        ("TENANT_UPDATED", "Tenant updated"),
        ("TEAM_MEMBER_ADDED", "Team member added"),
        ("TEAM_MEMBER_DEACTIVATED", "Team member deactivated"),
    ]

    id = models.BigAutoField(primary_key=True)
    actor = models.ForeignKey(
        PlatformAdmin, on_delete=models.SET_NULL, null=True, related_name="activity_logs"
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    restaurant = models.ForeignKey(
        "restaurant.Restaurant", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_activity_log"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} — {self.description}"
