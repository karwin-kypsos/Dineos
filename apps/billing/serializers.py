from decimal import Decimal

from rest_framework import serializers

from .models import Bill, CashierShift


class BillSerializer(serializers.ModelSerializer):
    processed_by_name = serializers.CharField(source="processed_by.name", read_only=True, default=None)
    table_number = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()
    restaurant_name = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    branch_address = serializers.SerializerMethodField()
    branch_phone = serializers.SerializerMethodField()
    gst_percentage = serializers.SerializerMethodField()
    service_charge_percentage = serializers.SerializerMethodField()

    def get_table_number(self, obj):
        return obj.session.table.table_number if obj.session_id else None

    def get_items(self, obj):
        from apps.orders.services import takeaway_group

        from . import services

        # A takeaway bill's order is always the root of its group (see
        # services.pay_takeaway_bill) — pull every round's items in, not
        # just the root's, so a multi-round takeaway receipt isn't missing
        # whatever was added in round 2/3/etc.
        orders = (
            takeaway_group(obj.order)
            if obj.order_id
            else obj.session.orders.exclude(status="CANCELLED").prefetch_related("items")
        )
        return services.line_items(orders)

    def _restaurant(self, obj):
        return obj.session.table.restaurant if obj.session_id else obj.order.branch.restaurant

    def _branch_info(self, obj):
        if not hasattr(obj, "_receipt_branch_info_cache"):
            from . import services

            obj._receipt_branch_info_cache = services.receipt_branch_info(obj.branch, self._restaurant(obj))
        return obj._receipt_branch_info_cache

    def get_restaurant_name(self, obj):
        return self._branch_info(obj)["restaurant_name"]

    def get_branch_name(self, obj):
        return self._branch_info(obj)["branch_name"]

    def get_branch_address(self, obj):
        return self._branch_info(obj)["branch_address"]

    def get_branch_phone(self, obj):
        return self._branch_info(obj)["branch_phone"]

    def get_gst_percentage(self, obj):
        return self._branch_info(obj)["gst_percentage"]

    def get_service_charge_percentage(self, obj):
        return self._branch_info(obj)["service_charge_percentage"]

    class Meta:
        model = Bill
        fields = [
            "id",
            "session",
            "order",
            "branch",
            "table_number",
            "restaurant_name",
            "branch_name",
            "branch_address",
            "branch_phone",
            "gst_percentage",
            "service_charge_percentage",
            "items",
            "subtotal",
            "tax_amount",
            "service_charge",
            "discount_amount",
            "total_amount",
            "payment_method",
            "amount_received",
            "change_given",
            "processed_by",
            "processed_by_name",
            "paid_at",
        ]
        read_only_fields = fields


class PayBillSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    payment_method = serializers.ChoiceField(choices=Bill.PaymentMethod.choices)
    amount_received = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)


class PayTakeawayBillSerializer(serializers.Serializer):
    order_id = serializers.UUIDField()
    payment_method = serializers.ChoiceField(choices=Bill.PaymentMethod.choices)
    amount_received = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)


class CashierShiftSerializer(serializers.ModelSerializer):
    cashier_name = serializers.CharField(source="cashier.name", read_only=True)

    class Meta:
        model = CashierShift
        fields = [
            "id",
            "branch",
            "cashier",
            "cashier_name",
            "status",
            "opened_at",
            "closed_at",
            "counted_cash",
            "discrepancy_acknowledged",
            "discrepancy_amount",
            "discrepancy_reason",
        ]
        read_only_fields = fields


class TableBillSummarySerializer(serializers.Serializer):
    """One row of Cashier Home's 'Awaiting Payment' / 'Active Tables' lists —
    built from a plain dict (`services.cashier_dashboard`), not a model.
    """

    session_id = serializers.UUIDField()
    table_id = serializers.UUIDField()
    table_number = serializers.CharField()
    total_amount = serializers.DecimalField(max_digits=10, decimal_places=2)


class CashierDashboardSerializer(serializers.Serializer):
    shift = CashierShiftSerializer(allow_null=True)
    collected_today = serializers.DecimalField(max_digits=10, decimal_places=2)
    awaiting_payment = TableBillSummarySerializer(many=True)
    active_tables = TableBillSummarySerializer(many=True)
    paid_today_count = serializers.IntegerField()


class ShiftReconciliationSerializer(serializers.Serializer):
    cash = serializers.DecimalField(max_digits=10, decimal_places=2)
    card = serializers.DecimalField(max_digits=10, decimal_places=2)
    upi = serializers.DecimalField(max_digits=10, decimal_places=2)
    total = serializers.DecimalField(max_digits=10, decimal_places=2)


class CloseShiftRequestSerializer(serializers.Serializer):
    counted_cash = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))
    acknowledge_discrepancy = serializers.BooleanField(default=False)
    discrepancy_reason = serializers.CharField(required=False, allow_blank=True, default="")


class DailyBillSerializer(serializers.ModelSerializer):
    table_number = serializers.CharField(source="session.table.table_number", read_only=True)

    class Meta:
        model = Bill
        fields = ["id", "table_number", "paid_at", "payment_method", "total_amount"]
        read_only_fields = fields


class PaymentBreakdownSerializer(serializers.Serializer):
    cash = serializers.DecimalField(max_digits=10, decimal_places=2)
    card = serializers.DecimalField(max_digits=10, decimal_places=2)
    upi = serializers.DecimalField(max_digits=10, decimal_places=2)


class DailyCollectionsSerializer(serializers.Serializer):
    date = serializers.DateField()
    total_collected = serializers.DecimalField(max_digits=10, decimal_places=2)
    vs_yesterday = serializers.DecimalField(max_digits=10, decimal_places=2)
    tables_served = serializers.IntegerField()
    tables_count = serializers.IntegerField()
    avg_bill_value = serializers.DecimalField(max_digits=10, decimal_places=2)
    largest_bill = serializers.DecimalField(max_digits=10, decimal_places=2)
    smallest_bill = serializers.DecimalField(max_digits=10, decimal_places=2)
    payment_breakdown = PaymentBreakdownSerializer()
    bills = DailyBillSerializer(many=True)
