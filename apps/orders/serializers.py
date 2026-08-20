from decimal import Decimal

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
            "ready_at",
            "collected_at",
            "served_at",
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
