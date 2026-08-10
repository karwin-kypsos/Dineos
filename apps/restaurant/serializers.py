from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import Branch, Restaurant

User = get_user_model()


class RestaurantSettingsSerializer(serializers.ModelSerializer):
    """Admin-only, tenant-side settings — deliberately a narrow field list.
    Everything else on Restaurant (plan_tier, status, max_branches, feature
    flags, org identity) stays Platform/Super-Admin-owned; this is the one
    slice a restaurant's own Admin gets to self-serve. Kept separate from
    apps.authentication.serializers.TenantSummarySerializer (used on /v1/
    auth/me/), which deliberately excludes billing rates from every staff
    role — see tests/test_authentication.py::test_me_does_not_expose_billing_rates.
    """

    class Meta:
        model = Restaurant
        fields = ["gst_percentage", "service_charge_percentage"]


class BranchManagerSummarySerializer(serializers.ModelSerializer):
    """Minimal staff summary for the manager field — avoids importing
    apps.authentication.serializers.UserSerializer, which imports back
    from this module (BranchSummarySerializer)."""

    class Meta:
        model = User
        fields = ["id", "name", "email"]


class BranchSerializer(serializers.ModelSerializer):
    manager = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False, allow_null=True)
    manager_detail = BranchManagerSummarySerializer(source="manager", read_only=True)
    tables = serializers.SerializerMethodField()

    class Meta:
        model = Branch
        fields = ["id", "name", "slug", "address", "phone", "photo_url", "manager", "manager_detail",
                  "table_count", "is_active", "created_at", "tables"]
        read_only_fields = ["id", "slug", "created_at"]

    def get_tables(self, obj):
        # Only on retrieve/create/update, not list — generating a QR PNG per
        # table is too costly to do for every branch on a list screen.
        if self.context.get("view") is not None and self.context["view"].action == "list":
            return None
        from apps.restaurant.services import build_table_qr

        result = []
        for table in obj.tables.filter(is_active=True).order_by("table_number"):
            qr_url, qr_code = build_table_qr(obj.restaurant.slug, obj.slug, table.table_number)
            result.append({
                "id": str(table.id),
                "table_number": table.table_number,
                "qr_url": qr_url,
                "qr_code": qr_code,
            })
        return result
        # "restaurant" isn't a serializer field (perform_create supplies it), so
        # DRF can't auto-build a UniqueTogetherValidator for one_branch_name_per_restaurant
        # — without this, a duplicate name reaches the DB constraint directly and
        # 500s instead of 400ing. Same pattern as apps.menu.CategorySerializer.
        validators = []

    def validate_name(self, value):
        request = self.context.get("request")
        restaurant = getattr(getattr(request, "user", None), "restaurant", None) if request else None
        if self.instance is not None:
            restaurant = restaurant or self.instance.restaurant
        if restaurant is None:
            return value
        conflict = Branch.objects.filter(restaurant=restaurant, name=value)
        if self.instance is not None:
            conflict = conflict.exclude(pk=self.instance.pk)
        if conflict.exists():
            raise serializers.ValidationError("A branch with this name already exists.")
        return value

    def validate_manager(self, manager):
        request = self.context.get("request")
        if manager is not None and request is not None and manager.restaurant_id != request.user.restaurant_id:
            raise serializers.ValidationError("Manager does not belong to your restaurant.")
        if manager is not None and manager.role != "MANAGER":
            raise serializers.ValidationError("Assigned manager must have the MANAGER role.")
        return manager


class BranchSummarySerializer(serializers.ModelSerializer):
    """Nested read-only representation used on Staff/User payloads."""

    class Meta:
        model = Branch
        fields = ["id", "name", "slug", "address", "photo_url", "is_active"]
