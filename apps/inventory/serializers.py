from decimal import Decimal

from rest_framework import serializers

from .models import AIInsight, Ingredient, PurchaseOrder, PurchaseOrderLine, RecipeItem, StockMovement


class IngredientSerializer(serializers.ModelSerializer):
    is_low_stock = serializers.BooleanField(read_only=True)
    stock_status = serializers.CharField(read_only=True)

    class Meta:
        model = Ingredient
        fields = [
            "id", "branch", "name", "unit", "current_stock", "unit_cost", "minimum_stock_level",
            "supplier_name", "supplier_phone", "supplier_notes", "is_low_stock", "stock_status", "is_active", "created_at",
        ]
        read_only_fields = ["id", "current_stock", "created_at"]
        validators = []  # conditional UniqueConstraints — see apps/menu CategorySerializer for why

    def validate_branch(self, value):
        # 2026-09-03 - branch had no tenant-ownership check at all: a
        # client could specify any restaurant's branch id and it would be
        # accepted outright.
        request = self.context.get("request")
        if value is not None and request is not None and value.restaurant_id != request.tenant.id:
            raise serializers.ValidationError("Branch does not belong to your restaurant.")
        return value

    def validate(self, attrs):
        # 2026-09-03 - moved from validate_name (field-level, ran before
        # "branch" was resolved) to here (object-level, sees the actual
        # branch attrs already validated above) - the uniqueness check
        # used to key off request.user.branch (the CALLER's own branch,
        # always None for Admin) instead of the branch actually being
        # written, so an Admin creating an ingredient for a specific
        # branch was checked against the wrong scope entirely.
        name = attrs.get("name", self.instance.name if self.instance else None)
        if name is None:
            return attrs
        branch = attrs["branch"] if "branch" in attrs else (self.instance.branch if self.instance else None)
        request = self.context.get("request")
        restaurant = getattr(request, "tenant", None) if request else None
        if restaurant is None and self.instance is not None:
            restaurant = self.instance.restaurant
        if branch is not None:
            conflict = Ingredient.objects.filter(branch=branch, name=name)
        elif restaurant is not None:
            conflict = Ingredient.objects.filter(restaurant=restaurant, name=name, branch__isnull=True)
        else:
            return attrs
        if self.instance is not None:
            conflict = conflict.exclude(pk=self.instance.pk)
        if conflict.exists():
            raise serializers.ValidationError("An ingredient with this name already exists.")
        return attrs


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
    estimated_total = serializers.SerializerMethodField()

    def get_estimated_total(self, obj):
        return sum((line.quantity_ordered * (line.unit_cost or Decimal("0")) for line in obj.lines.all()), Decimal("0"))

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "branch", "status", "reason", "is_emergency", "supplier_name", "supplier_notes",
            "requested_by", "requested_by_name", "approved_by", "approved_by_name",
            "approved_at", "created_at", "lines", "estimated_total",
        ]
        read_only_fields = fields


class PurchaseOrderCreateSerializer(serializers.Serializer):
    supplier_name = serializers.CharField(required=False, allow_blank=True, default="")
    supplier_notes = serializers.CharField(required=False, allow_blank=True, default="")
    reason = serializers.ChoiceField(choices=PurchaseOrder.Reason.choices, required=False, allow_blank=True, default="")
    is_emergency = serializers.BooleanField(required=False, default=False)
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


class AIInsightSerializer(serializers.ModelSerializer):
    ingredient_name = serializers.SerializerMethodField()
    unit = serializers.SerializerMethodField()

    def get_ingredient_name(self, obj):
        return obj.ingredient.name if obj.ingredient else None

    def get_unit(self, obj):
        return obj.ingredient.unit if obj.ingredient else None

    class Meta:
        model = AIInsight
        fields = [
            "id", "branch", "ingredient", "ingredient_name", "unit", "severity", "headline",
            "reason_breakdown", "recommended_action", "is_dismissed", "generated_at", "dismissed_at",
        ]
        read_only_fields = fields
