from rest_framework import serializers

from .models import Table, TableSession


class TableSerializer(serializers.ModelSerializer):
    active_session_id = serializers.SerializerMethodField()
    assigned_server_id = serializers.SerializerMethodField()
    assigned_server_name = serializers.SerializerMethodField()
    active_orders = serializers.SerializerMethodField()

    class Meta:
        model = Table
        fields = [
            "id", "branch", "table_number", "capacity", "status", "is_active", "active_session_id",
            "assigned_server_id", "assigned_server_name", "active_orders",
        ]

    def _active_session(self, obj):
        if not hasattr(obj, "_active_session_cache"):
            obj._active_session_cache = obj.sessions.filter(status__in=["ACTIVE", "BILL_REQUESTED"]).first()
        return obj._active_session_cache

    def get_active_session_id(self, obj):
        session = self._active_session(obj)
        return str(session.id) if session else None

    def get_assigned_server_id(self, obj):
        session = self._active_session(obj)
        return str(session.assigned_server_id) if session and session.assigned_server_id else None

    def get_assigned_server_name(self, obj):
        session = self._active_session(obj)
        return session.assigned_server.name if session and session.assigned_server_id else None

    def get_active_orders(self, obj):
        from apps.orders.serializers import OrderSerializer

        session = self._active_session(obj)
        if not session:
            return []
        return OrderSerializer(session.orders.exclude(status="CANCELLED").order_by("round_number"), many=True).data


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
