import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", User.Role.ADMIN)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        MANAGER = "MANAGER", "Manager"
        SERVER = "SERVER", "Server"
        CASHIER = "CASHIER", "Cashier"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="staff")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="staff"
    )
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    address = models.CharField(max_length=500, blank=True)
    role = models.CharField(max_length=16, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "users"
        ordering = ["-created_at"]

    def __str__(self):
        return self.email


def _generate_reset_token():
    from django.conf import settings

    if not settings.EMAIL_DELIVERY_ENABLED:
        return settings.COMMON_VERIFICATION_TOKEN
    return secrets.token_urlsafe(32)


class PasswordResetToken(models.Model):
    """token is deliberately NOT unique at the DB level: while
    EMAIL_DELIVERY_ENABLED is off, every row gets the same well-known
    value (see _generate_reset_token), so multiple pending invites/resets
    can coexist. Lookups always take the most recently issued match — see
    apps.authentication.views.ResetPasswordView — which is unambiguous
    once real per-user random tokens are turned on, and matches "the one
    I just triggered" while they're shared."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reset_tokens")
    token = models.CharField(max_length=64, default=_generate_reset_token, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "password_reset_tokens"
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, user, ttl_minutes=30, kind="reset"):
        token_obj = cls.objects.create(user=user, expires_at=timezone.now() + timedelta(minutes=ttl_minutes))

        from core.email import send_notification_email

        if kind == "invite":
            subject = "You're invited to DineOS"
            body = (
                f"Hi {user.name or 'there'},\n\n"
                f"You've been invited to join DineOS. Use this token to set your password and activate "
                f"your account:\n\n{token_obj.token}\n\n"
                f"This invite expires in {ttl_minutes // 1440} day(s)."
            )
        else:
            subject = "Reset your DineOS password"
            body = (
                f"Hi {user.name or 'there'},\n\n"
                f"Use this token to reset your DineOS password:\n\n{token_obj.token}\n\n"
                f"This token expires in {ttl_minutes} minutes. If you didn't request this, you can ignore this email."
            )
        send_notification_email(subject=subject, body=body, to_email=user.email)
        return token_obj

    @property
    def is_valid(self):
        return self.used_at is None and self.expires_at > timezone.now()
