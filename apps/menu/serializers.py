from decimal import Decimal

from rest_framework import serializers

from .models import Category, MenuItem, PreparedPortion
from .services import get_today_portion


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "branch", "name", "emoji", "sort_order", "is_active"]
        # Both constraints on Category are conditional (branch IS/IS NOT
        # NULL) — DRF's auto-generated UniqueTogetherValidator doesn't
        # understand conditional constraints and would reject a valid
        # update with a false-positive 400. Uniqueness is checked by hand
        # in validate_name() below instead.
        validators = []

    def validate_name(self, value):
        request = self.context.get("request")
        restaurant = getattr(request, "tenant", None) if request else None
        branch = getattr(getattr(request, "user", None), "branch", None) if request else None
        if self.instance is not None:
            restaurant = restaurant or self.instance.restaurant
            branch = self.instance.branch
        if branch is not None:
            conflict = Category.objects.filter(branch=branch, name=value)
        elif restaurant is not None:
            conflict = Category.objects.filter(restaurant=restaurant, name=value, branch__isnull=True)
        else:
            return value
        if self.instance is not None:
            conflict = conflict.exclude(pk=self.instance.pk)
        if conflict.exists():
            raise serializers.ValidationError("A category with this name already exists.")
        return value


class MenuItemSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    portions_remaining_today = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = [
            "id",
            "category",
            "category_name",
            "name",
            "description",
            "price",
            "image_url",
            "is_veg",
            "is_available",
            "is_active",
            "sort_order",
            "portions_remaining_today",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_portions_remaining_today(self, obj):
        portion = get_today_portion(obj)
        return portion.portions_remaining if portion else None


class MenuItemCustomerSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    portions_remaining = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = ["id", "category", "category_name", "name", "description", "price", "image_url", "is_veg", "portions_remaining"]

    def get_portions_remaining(self, obj):
        portion = get_today_portion(obj)
        return portion.portions_remaining if portion else None


class ToggleAvailabilitySerializer(serializers.Serializer):
    pass


class DeductionOverrideSerializer(serializers.Serializer):
    ingredient_id = serializers.UUIDField()
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))


class AddPortionsSerializer(serializers.Serializer):
    additional_quantity = serializers.IntegerField(min_value=1)
    # Override the recipe-computed ingredient deductions for this batch —
    # e.g. today's prep actually used more/less per portion than the
    # recipe says. Omit to use RecipeItem * additional_quantity as-is.
    deduction_overrides = DeductionOverrideSerializer(many=True, required=False)


class PreparedPortionSerializer(serializers.ModelSerializer):
    menu_item_name = serializers.CharField(source="menu_item.name", read_only=True)

    class Meta:
        model = PreparedPortion
        fields = ["id", "menu_item", "menu_item_name", "date", "portions_initial", "portions_remaining", "updated_at"]
