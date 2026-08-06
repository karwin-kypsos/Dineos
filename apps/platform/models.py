import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


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

    class AccessLevel(models.TextChoices):
        FULL_ADMIN = "FULL_ADMIN", "Full Admin"
        SUPPORT_READONLY = "SUPPORT_READONLY", "Support (Read-only)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    access_level = models.CharField(max_length=20, choices=AccessLevel.choices, default=AccessLevel.FULL_ADMIN)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=True)
    last_active_at = models.DateTimeField(null=True, blank=True)
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


def _generate_2fa_code():
    from django.conf import settings

    if not settings.EMAIL_DELIVERY_ENABLED:
        return settings.COMMON_VERIFICATION_CODE
    return f"{secrets.randbelow(1_000_000):06d}"


class PlatformLoginCode(models.Model):
    """Step 2 of Super Admin login — password already verified by the time
    this exists; the code just proves the caller also has access to the
    admin's registered contact channel. Short-lived and single-use, same
    shape as apps.authentication.models.PasswordResetToken."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin = models.ForeignKey(PlatformAdmin, on_delete=models.CASCADE, related_name="login_codes")
    code = models.CharField(max_length=6, default=_generate_2fa_code)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_login_codes"

    @classmethod
    def issue(cls, admin, ttl_minutes=5):
        return cls.objects.create(admin=admin, expires_at=timezone.now() + timedelta(minutes=ttl_minutes))

    @property
    def is_valid(self):
        return self.used_at is None and self.expires_at > timezone.now()


class ImpersonationSession(models.Model):
    """One 'Support Access' session — a Platform Admin logging in AS a
    tenant's own Admin. The access token minted for this carries an
    impersonation_session_id claim; core.tenancy.TenantResolverMiddleware
    checks this row on every request so ending a session here takes effect
    immediately, not just at token expiry (see 'explicit revoke' in the
    build guide's Impersonation section)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform_admin = models.ForeignKey(PlatformAdmin, on_delete=models.CASCADE, related_name="impersonation_sessions")
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="impersonation_sessions")
    target_user = models.ForeignKey("authentication.User", on_delete=models.CASCADE, related_name="impersonated_sessions")
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "impersonation_sessions"
        ordering = ["-started_at"]

    @property
    def is_active(self):
        return self.ended_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"{self.platform_admin} impersonating {self.target_user} ({self.restaurant})"


class PlatformActivityLog(models.Model):
    """Audit trail for actions taken by platform team members — powers the
    Super Admin app's Activity Log screen. Written by the views that
    perform the action (tenant create/update, team member added, etc.)
    rather than via signals, so the description text stays human-readable
    and action-specific instead of generic field-diff dumps.
    """

    ACTION_CHOICES = [
        ("TENANT_CREATED", "Org created"),
        ("TENANT_UPDATED", "Tenant updated"),
        ("STATUS_CHANGED", "Status changed"),
        ("FLAGS_CHANGED", "Flags changed"),
        ("PLAN_CHANGED", "Plan changed"),
        ("TEAM_MEMBER_ADDED", "Team member added"),
        ("TEAM_MEMBER_DEACTIVATED", "Team member deactivated"),
        ("TEAM_MEMBER_REMOVED", "Team member removed"),
        ("TENANT_IMPERSONATED", "Support access started"),
        ("IMPERSONATION_ENDED", "Support access ended"),
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


class PlatformAdminBlacklistedToken(models.Model):
    """Platform admins get an access-only token (see
    apps.platform.serializers.issue_platform_access_token) — there's no
    refresh token to blacklist via rest_framework_simplejwt's own
    token_blacklist app. Logout instead records the access token's own
    jti here; PlatformJWTAuthentication rejects any token whose jti shows
    up in this table, so a logged-out token stops working immediately
    rather than just quietly expiring later.
    """

    jti = models.CharField(max_length=64, unique=True)
    blacklisted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_admin_blacklisted_tokens"
