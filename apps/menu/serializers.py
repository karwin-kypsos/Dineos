from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from apps.inventory.serializers import RecipeItemSerializer
from core.image_fields import ImageUploadMixin

from .models import Category, MenuItem, PreparedPortion
from .services import get_today_portion


class RecipeItemInputSerializer(serializers.Serializer):
    ingredient = serializers.UUIDField()
    quantity_per_serving = serializers.DecimalField(max_digits=10, decimal_places=3, min_value=Decimal("0.001"))


class CategorySerializer(ImageUploadMixin, serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField()
    image = serializers.ImageField(write_only=True, required=False)

    class Meta:
        model = Category
        fields = ["id", "branch", "name", "emoji", "image_url", "image", "sort_order", "is_active", "item_count"]
        # Both constraints on Category are conditional (branch IS/IS NOT
        # NULL) — DRF's auto-generated UniqueTogetherValidator doesn't
        # understand conditional constraints and would reject a valid
        # update with a false-positive 400. Uniqueness is checked by hand
        # in validate_name() below instead.
        validators = []

    def get_item_count(self, obj):
        return obj.items.filter(is_active=True).count()

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


class MenuItemSerializer(ImageUploadMixin, serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    portions_remaining_today = serializers.SerializerMethodField()
    # Overrides the model field's default=0 (which DRF would otherwise
    # treat as optional) — the Add/Edit Menu Item screen always displays
    # dishes in priority order, so every item needs an explicit position
    # rather than silently landing at the same default as every other item.
    sort_order = serializers.IntegerField(min_value=0)
    image = serializers.ImageField(write_only=True, required=False)
    # "Recipe per Plate" section on the Add/Edit Menu Item screen — lets the
    # frontend submit the item's basic fields and its recipe ingredients in
    # one request instead of a create-item call followed by N separate
    # POST /v1/inventory/recipe-items/ calls. Each quantity is a plain
    # number in whatever unit the ingredient itself was defined with (KG/G/
    # L/ML/PCS) — see `unit` in the read-only `recipe` field below — there
    # is no separate per-line unit selector.
    recipe_items = RecipeItemInputSerializer(many=True, required=False, write_only=True)
    recipe = RecipeItemSerializer(source="recipe_items", many=True, read_only=True)

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
            "image",
            "is_veg",
            "is_available",
            "is_active",
            "sort_order",
            "portions_remaining_today",
            "recipe_items",
            "recipe",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_portions_remaining_today(self, obj):
        portion = get_today_portion(obj)
        return portion.portions_remaining if portion else None

    def validate_recipe_items(self, value):
        if not value:
            return value

        ingredient_ids = [line["ingredient"] for line in value]
        if len(ingredient_ids) != len(set(ingredient_ids)):
            raise serializers.ValidationError("Duplicate ingredient in recipe_items.")

        request = self.context.get("request")
        if request is not None:
            from apps.inventory.models import Ingredient

            valid_ids = set(
                Ingredient.objects.filter(id__in=ingredient_ids, restaurant=request.tenant).values_list("id", flat=True)
            )
            invalid = [str(i) for i in ingredient_ids if i not in valid_ids]
            if invalid:
                raise serializers.ValidationError(f"Ingredient(s) not found: {invalid}")
        return value

    @transaction.atomic
    def create(self, validated_data):
        validated_data = self._handle_image_upload(validated_data)
        recipe_items_data = validated_data.pop("recipe_items", [])
        menu_item = MenuItem.objects.create(**validated_data)
        self._save_recipe_items(menu_item, recipe_items_data)
        return menu_item

    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data = self._handle_image_upload(validated_data)
        recipe_items_data = validated_data.pop("recipe_items", None)
        # Bypass ImageUploadMixin.update (already handled the upload above)
        # and go straight to ModelSerializer.update — calling the mixin
        # again here would try to pop "image" a second time.
        instance = serializers.ModelSerializer.update(self, instance, validated_data)
        if recipe_items_data is not None:
            instance.recipe_items.all().delete()
            self._save_recipe_items(instance, recipe_items_data)
        return instance

    def _save_recipe_items(self, menu_item, recipe_items_data):
        from apps.inventory.models import RecipeItem

        RecipeItem.objects.bulk_create([
            RecipeItem(
                menu_item=menu_item, ingredient_id=line["ingredient"],
                quantity_per_serving=line["quantity_per_serving"],
            )
            for line in recipe_items_data
        ])


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
