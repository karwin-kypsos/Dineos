from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.menu.models import MenuItem

from .models import Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    menu_item_name = serializers.CharField(source="menu_item.name", read_only=True)
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = ["id", "menu_item", "menu_item_name", "quantity", "unit_price", "line_total", "notes", "status"]
        # status is set via PATCH /v1/orders/{order_id}/items/{item_id}/status/
        # (apps.orders.views.OrderItemKitchenStatusView), not through this
        # serializer directly — kept read-only here same as id/unit_price/line_total.
        read_only_fields = ["id", "unit_price", "line_total", "status"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    total_amount = serializers.SerializerMethodField()

    def get_total_amount(self, obj):
        return sum((item.line_total for item in obj.items.all()), Decimal("0"))

    class Meta:
        model = Order
        fields = [
            "id",
            "order_type",
            "session",
            "table",
            "branch",
            "customer_name",
            "customer_phone",
            "round_number",
            "status",
            "placed_by",
            "notes",
            "items",
            "total_amount",
            "placed_at",
            "accepted_at",
            "preparing_at",
            "ready_at",
            "collected_at",
            "served_at",
        ]
        read_only_fields = fields


class KDSOrderSerializer(OrderSerializer):
    """GET /v1/orders/active/'s per-order shape — everything OrderSerializer
    already has, plus the KDS "Live" dashboard card metrics Shereena's KOT
    Live spec asked for (2026-08-21): a display-ready table_number, wait-time
    fields, and a per-order item-status breakdown. Kept as a subclass (rather
    than bolting these onto OrderSerializer itself) so every OTHER consumer
    of OrderSerializer — order creation, order detail, ready orders, etc. —
    is untouched; only this one endpoint's response shape changes.
    """

    table_number = serializers.SerializerMethodField()
    elapsed_seconds = serializers.SerializerMethodField()
    elapsed_formatted = serializers.SerializerMethodField()
    is_urgent = serializers.SerializerMethodField()
    total_items_count = serializers.SerializerMethodField()
    pending_items_count = serializers.SerializerMethodField()
    cooking_items_count = serializers.SerializerMethodField()
    ready_items_count = serializers.SerializerMethodField()

    # "URGENT badge (true if wait time > 10m)" per Shereena's spec.
    URGENT_THRESHOLD_SECONDS = 600

    def get_table_number(self, obj):
        # Takeaway orders have no table at all; dine-in ones always do
        # (enforced by the dine_in_has_table_takeaway_does_not constraint).
        # There's no established convention yet for what a takeaway card's
        # "table_number" should read — using the plain "Takeaway" literal
        # here is this session's interpretation, flagged to Shereena.
        if obj.table_id is None:
            return "Takeaway"
        return f"Table {obj.table.table_number}"

    def _elapsed_seconds(self, obj):
        # Cached per-instance so elapsed_seconds/elapsed_formatted/is_urgent
        # (three separate SerializerMethodField calls) share one "now" and
        # one subtraction instead of drifting a few microseconds apart.
        if not hasattr(obj, "_kds_elapsed_seconds_cache"):
            obj._kds_elapsed_seconds_cache = max(0, int((timezone.now() - obj.placed_at).total_seconds()))
        return obj._kds_elapsed_seconds_cache

    def get_elapsed_seconds(self, obj):
        return self._elapsed_seconds(obj)

    def get_elapsed_formatted(self, obj):
        minutes, seconds = divmod(self._elapsed_seconds(obj), 60)
        return f"{minutes:02d}:{seconds:02d}"  # MM:SS — MM allowed to exceed 99 for old demo orders

    def get_is_urgent(self, obj):
        return self._elapsed_seconds(obj) > self.URGENT_THRESHOLD_SECONDS

    def _items(self, obj):
        # obj.items.all() hits the view's prefetch_related("items") cache —
        # no extra query per order — so this stays cheap on a "runs on every
        # KDS refresh" endpoint even with many active orders.
        if not hasattr(obj, "_kds_items_cache"):
            obj._kds_items_cache = list(obj.items.all())
        return obj._kds_items_cache

    def get_total_items_count(self, obj):
        return len(self._items(obj))

    def get_pending_items_count(self, obj):
        return sum(1 for item in self._items(obj) if item.status == Order.Status.NEW)

    def get_cooking_items_count(self, obj):
        return sum(
            1 for item in self._items(obj) if item.status in (Order.Status.ACCEPTED, Order.Status.PREPARING)
        )

    def get_ready_items_count(self, obj):
        return sum(1 for item in self._items(obj) if item.status == Order.Status.READY)

    class Meta(OrderSerializer.Meta):
        fields = OrderSerializer.Meta.fields + [
            "table_number",
            "elapsed_seconds",
            "elapsed_formatted",
            "is_urgent",
            "total_items_count",
            "pending_items_count",
            "cooking_items_count",
            "ready_items_count",
        ]
        read_only_fields = fields


class OrderItemInputSerializer(serializers.Serializer):
    menu_item = serializers.PrimaryKeyRelatedField(queryset=MenuItem.objects.all())
    quantity = serializers.IntegerField(min_value=1)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class OrderCreateSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    items = OrderItemInputSerializer(many=True)


class TakeawayOrderCreateSerializer(serializers.Serializer):
    customer_name = serializers.CharField(required=False, allow_blank=True, default="")
    customer_phone = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    items = OrderItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        return value


class OrderStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["accepted", "preparing", "ready"])

    def validate_status(self, value):
        return value.upper()
