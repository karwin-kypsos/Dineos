from rest_framework import serializers

from apps.menu.models import MenuItem

from .models import Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    menu_item_name = serializers.CharField(source="menu_item.name", read_only=True)
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = ["id", "menu_item", "menu_item_name", "quantity", "unit_price", "line_total", "notes"]
        read_only_fields = ["id", "unit_price", "line_total"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "session",
            "table",
            "round_number",
            "status",
            "placed_by",
            "notes",
            "items",
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


class OrderStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["accepted", "preparing", "ready"])

    def validate_status(self, value):
        return value.upper()
