from decimal import Decimal

from rest_framework import serializers

from .models import Ingredient, PurchaseOrder, PurchaseOrderLine, RecipeItem, StockMovement


class IngredientSerializer(serializers.ModelSerializer):
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Ingredient
        fields = [
            "id", "branch", "name", "unit", "current_stock", "unit_cost", "minimum_stock_level",
            "supplier_name", "supplier_notes", "is_low_stock", "is_active", "created_at",
        ]
        read_only_fields = ["id", "current_stock", "created_at"]
        validators = []  # conditional UniqueConstraints — see apps/menu CategorySerializer for why

    def validate_name(self, value):
        request = self.context.get("request")
        restaurant = getattr(request, "tenant", None) if request else None
        branch = getattr(getattr(request, "user", None), "branch", None) if request else None
        if self.instance is not None:
            restaurant = restaurant or self.instance.restaurant
            branch = self.instance.branch
        if branch is not None:
            conflict = Ingredient.objects.filter(branch=branch, name=value)
        elif restaurant is not None:
            conflict = Ingredient.objects.filter(restaurant=restaurant, name=value, branch__isnull=True)
        else:
            return value
        if self.instance is not None:
            conflict = conflict.exclude(pk=self.instance.pk)
        if conflict.exists():
            raise serializers.ValidationError("An ingredient with this name already exists.")
        return value


class StockMovementSerializer(serializers.ModelSerializer):
    ingredient_name = serializers.CharField(source="ingredient.name", read_only=True)
    recorded_by_name = serializers.CharField(source="recorded_by.name", read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            "id", "ingredient", "ingredient_name", "movement_type", "quantity",
            "wastage_reason", "reason", "unit_cost_at_time", "recorded_by", "recorded_by_name", "recorded_at",
        ]
        read_only_fields = fields


class AddStockSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    unit_cost = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)


class RecordWastageSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    wastage_reason = serializers.ChoiceField(choices=StockMovement.WastageReason.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    ingredient_name = serializers.CharField(source="ingredient.name", read_only=True)
    unit = serializers.CharField(source="ingredient.unit", read_only=True)

    class Meta:
        model = PurchaseOrderLine
        fields = ["id", "ingredient", "ingredient_name", "unit", "quantity_ordered", "quantity_received", "unit_cost"]
        read_only_fields = ["id", "quantity_received"]


class PurchaseOrderLineInputSerializer(serializers.Serializer):
    ingredient = serializers.PrimaryKeyRelatedField(queryset=Ingredient.objects.all())
    quantity_ordered = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    unit_cost = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)


class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = PurchaseOrderLineSerializer(many=True, read_only=True)
    requested_by_name = serializers.CharField(source="requested_by.name", read_only=True)
    approved_by_name = serializers.CharField(source="approved_by.name", read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "branch", "status", "supplier_name", "supplier_notes",
            "requested_by", "requested_by_name", "approved_by", "approved_by_name",
            "approved_at", "created_at", "lines",
        ]
        read_only_fields = fields


class PurchaseOrderCreateSerializer(serializers.Serializer):
    supplier_name = serializers.CharField(required=False, allow_blank=True, default="")
    supplier_notes = serializers.CharField(required=False, allow_blank=True, default="")
    lines = PurchaseOrderLineInputSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("At least one line item is required.")
        return value


class RecipeItemSerializer(serializers.ModelSerializer):
    menu_item_name = serializers.CharField(source="menu_item.name", read_only=True)
    ingredient_name = serializers.CharField(source="ingredient.name", read_only=True)
    unit = serializers.CharField(source="ingredient.unit", read_only=True)

    class Meta:
        model = RecipeItem
        fields = ["id", "menu_item", "menu_item_name", "ingredient", "ingredient_name", "unit", "quantity_per_serving"]
