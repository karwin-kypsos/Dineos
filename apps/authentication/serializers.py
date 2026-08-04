from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.restaurant.models import Restaurant

User = get_user_model()

# Role → (numeric id, external role-name slug). The Postman collection, mobile
# clients, and any external consumers that want a stable numeric role identifier
# or a lowercase slug read from these. The DB still stores the enum string
# (User.Role.ADMIN etc.), so this mapping is a projection layer only.
ROLE_METADATA = {
    "ADMIN":   {"id": 1, "name": "org_admin"},
    "MANAGER": {"id": 2, "name": "manager"},
    "SERVER":  {"id": 3, "name": "server"},
    "CASHIER": {"id": 4, "name": "cashier"},
}


class TenantSummarySerializer(serializers.ModelSerializer):
    """The subset of a tenant's record staff clients need to render a
    feature-flag-aware UI — not the full platform-management view
    (see apps.platform.serializers.RestaurantSerializer for that).
    """

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "notifications_enabled",
            "kitchen_enabled",
            "billing_enabled",
            "realtime_enabled",
        ]


class DineOSTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["name"] = user.name
        token["restaurant_id"] = str(user.restaurant_id)
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["role"] = self.user.role
        data["role_id"] = ROLE_METADATA[self.user.role]["id"]
        data["role_name"] = ROLE_METADATA[self.user.role]["name"]
        data["name"] = self.user.name
        data["restaurant_id"] = str(self.user.restaurant_id)
        return data


class UserSerializer(serializers.ModelSerializer):
    restaurant = TenantSummarySerializer(read_only=True)
    role_id = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "name", "phone", "role", "role_id", "role_name",
                  "is_active", "restaurant", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_role_id(self, obj):
        return ROLE_METADATA[obj.role]["id"]

    def get_role_name(self, obj):
        return ROLE_METADATA[obj.role]["name"]


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "name", "phone", "role", "password"]

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.CharField()
    new_password = serializers.CharField(min_length=8)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField()
    new_password = serializers.CharField(min_length=8)
