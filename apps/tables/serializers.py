from rest_framework import serializers

from .models import Table, TableSession


class TableSerializer(serializers.ModelSerializer):
    active_session_id = serializers.SerializerMethodField()

    class Meta:
        model = Table
        fields = ["id", "table_number", "capacity", "status", "is_active", "active_session_id"]

    def get_active_session_id(self, obj):
        session = obj.sessions.filter(status__in=["ACTIVE", "BILL_REQUESTED"]).first()
        return str(session.id) if session else None


class TableSessionSerializer(serializers.ModelSerializer):
    table_number = serializers.CharField(source="table.table_number", read_only=True)

    class Meta:
        model = TableSession
        fields = ["id", "table", "table_number", "status", "opened_at", "closed_at", "close_reason"]
        read_only_fields = fields


class TableSessionDetailSerializer(serializers.ModelSerializer):
    table_number = serializers.CharField(source="table.table_number", read_only=True)
    orders = serializers.SerializerMethodField()
    running_total = serializers.SerializerMethodField()

    class Meta:
        model = TableSession
        fields = ["id", "table", "table_number", "status", "opened_at", "orders", "running_total"]

    def get_orders(self, obj):
        from apps.orders.serializers import OrderSerializer

        return OrderSerializer(obj.orders.all().order_by("round_number"), many=True).data

    def get_running_total(self, obj):
        total = 0
        for order in obj.orders.exclude(status="CANCELLED").prefetch_related("items"):
            for item in order.items.all():
                total += item.unit_price * item.quantity
        return total


class QRLandingSerializer(serializers.Serializer):
    table = TableSerializer()
    active_session = TableSessionSerializer(allow_null=True)


class ManagerStatusOverrideSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Table.Status.choices)
    mark_unpaid = serializers.BooleanField(default=False)
