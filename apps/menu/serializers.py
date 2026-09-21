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
    """2026-09-21, per Shereena: is_available is now the EFFECTIVE answer,
    not just the stored toggle.

    A dish whose recipe needs an ingredient at or below zero cannot be
    made, so it must stop being sellable on every client at once. The
    stored column stays the manual switch a manager flips by hand; what
    goes out over the API is that AND "no recipe ingredient has run
    out". Computing it here rather than writing the column keeps it
    self-healing: restock the ingredient and the dish comes back with
    nothing to repair, and no background job to run.

    unavailable_reason says WHY, so the app can show "out of stock:
    Chicken" rather than an unexplained grey row.

    Set the `blocked_items` context (menu_item_id -> [ingredient names],
    from apps.menu.services.out_of_stock_ingredients) to resolve a whole
    page in one query. Without it each row falls back to its own lookup,
    which is correct but N+1 - list views should always pass it.
    """

    is_available = serializers.SerializerMethodField()
    unavailable_reason = serializers.SerializerMethodField()

    def _blocking_ingredients(self, obj):
        blocked = self.context.get("blocked_items")
        if blocked is not None:
            return blocked.get(obj.id, [])
        from .services import out_of_stock_ingredients

        return out_of_stock_ingredients([obj.id]).get(obj.id, [])

    def get_is_available(self, obj):
        if not obj.is_available:
            return False
        return not self._blocking_ingredients(obj)

    def get_unavailable_reason(self, obj):
        if not obj.is_available:
            return "Turned off manually"
        names = self._blocking_ingredients(obj)
        if names:
            return "Out of stock: " + ", ".join(sorted(names))
        return ""

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
            "unavailable_reason",
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
    # 2026-09-21: same effective-availability rule as the staff
    # serializer. The sellable queryset already filters 86'd dishes out
    # of this view, so in practice is_available is true for everything
    # returned here - it is kept honest rather than hardcoded because
    # this serializer is also used where the caller supplies its own
    # queryset, and a stale true would be worse than useless.
    is_available = serializers.SerializerMethodField()
    unavailable_reason = serializers.SerializerMethodField()

    def _blocking_ingredients(self, obj):
        blocked = self.context.get("blocked_items")
        if blocked is not None:
            return blocked.get(obj.id, [])
        from .services import out_of_stock_ingredients

        return out_of_stock_ingredients([obj.id]).get(obj.id, [])

    def get_is_available(self, obj):
        if not obj.is_available:
            return False
        return not self._blocking_ingredients(obj)

    def get_unavailable_reason(self, obj):
        if not obj.is_available:
            return "Turned off manually"
        names = self._blocking_ingredients(obj)
        if names:
            return "Out of stock: " + ", ".join(sorted(names))
        return ""

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
