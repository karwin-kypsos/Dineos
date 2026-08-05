from rest_framework import serializers
from rest_framework_simplejwt.tokens import AccessToken

from apps.restaurant.models import Restaurant

from .models import PlatformActivityLog, PlatformAdmin


def issue_platform_access_token(admin):
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
    return token


class PlatformLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        admin = PlatformAdmin.objects.filter(email=attrs["email"], is_active=True).first()
        if admin is None or not admin.check_password(attrs["password"]):
            raise serializers.ValidationError("Invalid email or password.")
        attrs["admin"] = admin
        return attrs


class RestaurantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "is_active",
            "gst_percentage",
            "service_charge_percentage",
            "notifications_enabled",
            "kitchen_enabled",
            "billing_enabled",
            "realtime_enabled",
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
        fields = ["id", "email", "name", "is_active", "is_staff", "created_at", "password"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        admin = PlatformAdmin(**validated_data)
        if password:
            admin.set_password(password)
        else:
            admin.set_unusable_password()
        admin.save()
        return admin


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
