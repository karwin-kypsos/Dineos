from rest_framework import serializers

from .models import Category, MenuItem, PreparedPortion
from .services import get_today_portion


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "emoji", "sort_order", "is_active"]

    def validate_name(self, value):
        # `restaurant` isn't a serializer field (it's set from request.tenant,
        # never client-supplied), so DRF can't auto-build a validator for the
        # UniqueConstraint(["restaurant", "name"]) — check it explicitly here,
        # otherwise a colliding rename surfaces as a raw 500 IntegrityError.
        request = self.context.get("request")
        restaurant = getattr(request, "tenant", None) if request else None
        if restaurant is None and self.instance is not None:
            restaurant = self.instance.restaurant
        if restaurant is not None:
            conflict = Category.objects.filter(restaurant=restaurant, name=value)
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
            "is_available",
            "is_active",
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
        fields = ["id", "category", "category_name", "name", "description", "price", "image_url", "portions_remaining"]

    def get_portions_remaining(self, obj):
        portion = get_today_portion(obj)
        return portion.portions_remaining if portion else None


class ToggleAvailabilitySerializer(serializers.Serializer):
    pass


class AddPortionsSerializer(serializers.Serializer):
    additional_quantity = serializers.IntegerField(min_value=1)


class PreparedPortionSerializer(serializers.ModelSerializer):
    menu_item_name = serializers.CharField(source="menu_item.name", read_only=True)

    class Meta:
        model = PreparedPortion
        fields = ["id", "menu_item", "menu_item_name", "date", "portions_initial", "portions_remaining", "updated_at"]
