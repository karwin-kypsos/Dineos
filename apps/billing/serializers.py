from decimal import Decimal

from rest_framework import serializers

from .models import Bill, CashierShift


class BillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bill
        fields = [
            "id",
            "session",
            "subtotal",
            "tax_amount",
            "service_charge",
            "discount_amount",
            "total_amount",
            "payment_method",
            "processed_by",
            "paid_at",
        ]
        read_only_fields = fields


class PayBillSerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    payment_method = serializers.ChoiceField(choices=Bill.PaymentMethod.choices)
    amount_received = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)


class CashierShiftSerializer(serializers.ModelSerializer):
    cashier_name = serializers.CharField(source="cashier.name", read_only=True)

    class Meta:
        model = CashierShift
        fields = [
            "id",
            "cashier",
            "cashier_name",
            "status",
            "opened_at",
            "closed_at",
            "counted_cash",
            "discrepancy_acknowledged",
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
    avg_bill_value = serializers.DecimalField(max_digits=10, decimal_places=2)
    largest_bill = serializers.DecimalField(max_digits=10, decimal_places=2)
    smallest_bill = serializers.DecimalField(max_digits=10, decimal_places=2)
    payment_breakdown = PaymentBreakdownSerializer()
    bills = DailyBillSerializer(many=True)
