from rest_framework import serializers
from rest_framework_simplejwt.tokens import AccessToken

from apps.restaurant.models import Restaurant

from .models import ImpersonationSession, PlatformActivityLog, PlatformAdmin, PlatformLoginCode


def issue_platform_access_token(admin, extra_claims=None):
    """Platform admins get an access-only token, minted directly via
    AccessToken.for_user() — NOT RefreshToken.for_user()/TokenObtainPairSerializer.
    RefreshToken.for_user() always writes an OutstandingToken row FK'd to
    settings.AUTH_USER_MODEL (apps.authentication.User) because the
    token_blacklist app is installed; PlatformAdmin is a different model
    entirely, so that write fails outright. Sidestepping refresh tokens
    for platform admins avoids that conflict rather than fighting it.
    """
    token = AccessToken.for_user(admin)
    token["platform_admin"] = True
    token["name"] = admin.name
    for key, value in (extra_claims or {}).items():
        token[key] = value
    return token


class PlatformLoginSerializer(serializers.Serializer):
    """Step 1 of 2 — password only. A correct password issues a 2FA code
    (see PlatformLoginCode) rather than a token; step 2 is
    VerifyPlatformLoginCodeSerializer."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        admin = PlatformAdmin.objects.filter(email=attrs["email"], is_active=True).first()
        if admin is None or not admin.check_password(attrs["password"]):
            raise serializers.ValidationError("Invalid email or password.")
        attrs["admin"] = admin
        return attrs


class VerifyPlatformLoginCodeSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6, min_length=6)

    def validate(self, attrs):
        admin = PlatformAdmin.objects.filter(email=attrs["email"], is_active=True).first()
        if admin is None:
            raise serializers.ValidationError("Invalid email or code.")
        login_code = PlatformLoginCode.objects.filter(admin=admin, code=attrs["code"]).order_by("-created_at").first()
        if not login_code or not login_code.is_valid:
            raise serializers.ValidationError("Invalid or expired code.")
        attrs["admin"] = admin
        attrs["login_code"] = login_code
        return attrs


class RestaurantSerializer(serializers.ModelSerializer):
    branch_count = serializers.IntegerField(source="branches.count", read_only=True)
    staff_count = serializers.IntegerField(source="staff.count", read_only=True)

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "is_active",
            "status",
            "contact_name",
            "contact_email",
            "contact_phone",
            "billing_email",
            "primary_color",
            "plan_tier",
            "max_branches",
            "default_manager_spending_limit",
            "gst_percentage",
            "service_charge_percentage",
            "notifications_enabled",
            "kitchen_enabled",
            "billing_enabled",
            "realtime_enabled",
            "branch_count",
            "staff_count",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class PlatformAdminSerializer(serializers.ModelSerializer):
    """Team screen — list/create platform team members. Password is
    write-only and hashed via set_password, never round-tripped in a
    response, matching the pattern used by apps.authentication.StaffSerializer.
    """

    password = serializers.CharField(write_only=True, required=False, min_length=8)

    class Meta:
        model = PlatformAdmin
        fields = ["id", "email", "name", "access_level", "is_active", "is_staff", "last_active_at", "created_at", "password"]
        read_only_fields = ["id", "last_active_at", "created_at"]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        admin = PlatformAdmin(**validated_data)
        if password:
            admin.set_password(password)
        else:
            admin.set_unusable_password()
        admin.save()
        return admin


class ImpersonationSessionSerializer(serializers.ModelSerializer):
    platform_admin_email = serializers.CharField(source="platform_admin.email", read_only=True)
    restaurant_name = serializers.CharField(source="restaurant.name", read_only=True)
    target_user_email = serializers.CharField(source="target_user.email", read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = ImpersonationSession
        fields = [
            "id", "platform_admin", "platform_admin_email", "restaurant", "restaurant_name",
            "target_user", "target_user_email", "started_at", "expires_at", "ended_at", "is_active",
        ]
        read_only_fields = fields


class PlatformActivityLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.name", read_only=True, default="")
    actor_email = serializers.CharField(source="actor.email", read_only=True, default="")
    restaurant_name = serializers.CharField(source="restaurant.name", read_only=True, default="")

    class Meta:
        model = PlatformActivityLog
        fields = [
            "id",
            "action",
            "description",
            "actor_name",
            "actor_email",
            "restaurant_name",
            "created_at",
        ]
        read_only_fields = fields
