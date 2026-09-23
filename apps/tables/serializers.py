from decimal import Decimal

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
    subtotal = serializers.SerializerMethodField()
    tax_amount = serializers.SerializerMethodField()
    service_charge = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()

    class Meta:
        model = TableSession
        fields = [
            "id", "table", "table_number", "status", "opened_at", "orders", "running_total",
            "subtotal", "tax_amount", "service_charge", "total_amount",
        ]

    def get_orders(self, obj):
        from apps.orders.serializers import OrderSerializer

        return OrderSerializer(obj.orders.all().order_by("round_number"), many=True).data

    def get_running_total(self, obj):
        # 2026-09-23: every money field on this serializer is a decimal
        # STRING now. This is the customer's own bill preview - what they
        # are told they owe - and it was going out as float: 300.0, 15.0,
        # 315.0. Floats are the wrong carrier for money anywhere, and
        # worst of all on the screen where someone decides what to pay.
        total = Decimal("0")
        for order in obj.orders.exclude(status="CANCELLED").prefetch_related("items"):
            for item in order.items.all():
                total += item.unit_price * item.quantity
        return str(total.quantize(Decimal("0.01")))

    def _totals(self, obj):
        # subtotal/tax_amount/service_charge/total_amount (2026-08-27, per
        # Shereena — the customer app's bill preview only had running_total,
        # a raw item-price sum with no GST/service charge, so what the
        # customer saw never matched what they'd actually be asked to pay.
        # Reuses the exact same calculation the staff-side bill preview and
        # the final paid Bill use (apps.billing.services._compute_totals),
        # so the customer sees the true final amount, not an approximation.
        # Cached per-instance since 3 separate SerializerMethodFields all
        # need it.
        if not hasattr(obj, "_tax_totals_cache"):
            from apps.billing.services import _compute_totals

            subtotal, tax_amount, service_charge, total_amount, _orders = _compute_totals(obj)
            obj._tax_totals_cache = (subtotal, tax_amount, service_charge, total_amount)
        return obj._tax_totals_cache

    def get_subtotal(self, obj):
        return str(self._totals(obj)[0].quantize(Decimal("0.01")))

    def get_tax_amount(self, obj):
        return str(self._totals(obj)[1].quantize(Decimal("0.01")))

    def get_service_charge(self, obj):
        return str(self._totals(obj)[2].quantize(Decimal("0.01")))

    def get_total_amount(self, obj):
        return str(self._totals(obj)[3].quantize(Decimal("0.01")))


class QRLandingSerializer(serializers.Serializer):
    table = TableSerializer()
    active_session = TableSessionSerializer(allow_null=True)


class ManagerStatusOverrideSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Table.Status.choices)
    mark_unpaid = serializers.BooleanField(default=False)
