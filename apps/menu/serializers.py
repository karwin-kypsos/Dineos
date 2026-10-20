import json
from decimal import Decimal

from django.db import transaction
from rest_framework import serializers
from rest_framework.fields import empty
from rest_framework.utils import html

from apps.inventory.serializers import RecipeItemSerializer
from core.image_fields import ImageUploadMixin

from .models import Category, MenuItem, PreparedPortion
from .services import get_today_portion


class RecipeItemInputListSerializer(serializers.ListSerializer):
    """recipe_items on a multipart request (2026-10-05, per Karwin). A
    form can only carry text, so the app sends the list as one
    JSON-encoded string next to the image. DRF reads a nested list from a
    form only as recipe_items[0]ingredient=... keys, so the string was
    silently dropped: the item saved with no recipe and no error."""

    def get_value(self, dictionary):
        raw = dictionary.get(self.field_name) if html.is_html_input(dictionary) else None
        if isinstance(raw, str):
            if not raw.strip():
                return empty  # blank: not sent - an edit leaves the recipe alone
            try:
                return json.loads(raw)
            except ValueError:
                return raw  # answered as "Expected a list of items", not dropped
        return super().get_value(dictionary)


class RecipeItemInputSerializer(serializers.Serializer):
    ingredient = serializers.UUIDField()
    quantity_per_serving = serializers.DecimalField(max_digits=10, decimal_places=3, min_value=Decimal("0.001"))

    class Meta:
        list_serializer_class = RecipeItemInputListSerializer


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
        # written, so an Admin creating a category for a specific branch
        # was checked against the wrong scope entirely.
        name = attrs.get("name", self.instance.name if self.instance else None)
        if name is None:
            return attrs
        branch = attrs["branch"] if "branch" in attrs else (self.instance.branch if self.instance else None)
        request = self.context.get("request")
        # 2026-10-05: a Manager's new category is always filed under the
        # Manager's own branch (CategoryViewSet.perform_create) - check that
        # branch, or a duplicate hit the unique constraint as a 500.
        user_branch = getattr(getattr(request, "user", None), "branch", None) if request else None
        if self.instance is None and user_branch is not None:
            branch = user_branch
        restaurant = getattr(request, "tenant", None) if request else None
        if restaurant is None and self.instance is not None:
            restaurant = self.instance.restaurant
        if branch is not None:
            conflict = Category.objects.filter(branch=branch, name=name)
        elif restaurant is not None:
            conflict = Category.objects.filter(restaurant=restaurant, name=name, branch__isnull=True)
        else:
            return attrs
        if self.instance is not None:
            conflict = conflict.exclude(pk=self.instance.pk)
        if conflict.exists():
            raise serializers.ValidationError("A category with this name already exists.")
        return attrs


class MenuItemSerializer(ImageUploadMixin, serializers.ModelSerializer):
    """is_available is the Manager's own toggle and nothing else (2026-10-20,
    per Karwin). From 2026-09-21 it was "toggle AND no recipe ingredient has
    run out", so a dish disappeared when stock hit zero and a Manager's
    "make it available anyway" was silently overridden by the next stock
    change. Stock is now information for the Manager to act on:

      stock_status             healthy / low / critical (= out), null without a recipe
      stock_warning            "Out of stock: Chicken" / "Low stock: Rice" / ""
      out_of_stock_ingredients recipe ingredients at or below zero
      low_stock_ingredients    recipe ingredients at or below their reorder point

    Set the `recipe_stock` context (apps.menu.services.recipe_stock) to
    resolve a whole page in one query; without it each row looks itself up.
    """

    is_available = serializers.BooleanField(read_only=True)
    unavailable_reason = serializers.SerializerMethodField()
    stock_status = serializers.SerializerMethodField()
    stock_warning = serializers.SerializerMethodField()
    out_of_stock_ingredients = serializers.SerializerMethodField()
    low_stock_ingredients = serializers.SerializerMethodField()

    def _stock(self, obj):
        stock = self.context.get("recipe_stock")
        if stock is None:
            from .services import recipe_stock

            stock = recipe_stock([obj.id])
        return stock.get(obj.id)  # None: no recipe

    def get_unavailable_reason(self, obj):
        return "" if obj.is_available else "Turned off manually"

    def get_stock_status(self, obj):
        stock = self._stock(obj)
        if stock is None:
            return None
        return "critical" if stock["critical"] else "low" if stock["low"] else "healthy"

    def get_stock_warning(self, obj):
        stock = self._stock(obj) or {"critical": [], "low": []}
        parts = []
        if stock["critical"]:
            parts.append("Out of stock: " + ", ".join(stock["critical"]))
        if stock["low"]:
            parts.append("Low stock: " + ", ".join(stock["low"]))
        return "; ".join(parts)

    def get_out_of_stock_ingredients(self, obj):
        return (self._stock(obj) or {}).get("critical", [])

    def get_low_stock_ingredients(self, obj):
        return (self._stock(obj) or {}).get("low", [])

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
    # L/ML/PIECES/PORTION) — see `unit` in the read-only `recipe` field below — there
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
            "unavailable_reason",
            "stock_status",
            "stock_warning",
            "out_of_stock_ingredients",
            "low_stock_ingredients",
            "is_active",
            "sort_order",
            "tracks_daily_portions",
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
    # The Manager's toggle alone since 2026-10-20 (no stock override) - see
    # MenuItemSerializer. The customer QR menu carries no stock details.
    is_available = serializers.BooleanField(read_only=True)
    unavailable_reason = serializers.SerializerMethodField()

    def get_unavailable_reason(self, obj):
        return "" if obj.is_available else "Turned off manually"

    class Meta:
        model = MenuItem
        fields = [
            "id", "category", "category_name", "name", "description", "price", "image_url", "is_veg",
            "is_available", "unavailable_reason", "tracks_daily_portions", "portions_remaining",
        ]

    def get_portions_remaining(self, obj):
        portion = get_today_portion(obj)
        return portion.portions_remaining if portion else None


class CategoryCustomerSerializer(serializers.ModelSerializer):
    """GET /v1/menu/categories/customer/{table_id}/ (2026-08-25, per
    Shereena) — the public-facing category-tab list for the Customer Web
    App, parallel to MenuItemCustomerSerializer. Deliberately its own
    serializer rather than reusing the staff CategorySerializer, which
    exposes branch/is_active/item_count and needs auth to reach at all."""

    class Meta:
        model = Category
        fields = ["id", "name", "emoji", "image_url", "sort_order"]


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
