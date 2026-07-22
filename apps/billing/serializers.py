from rest_framework import serializers

from .models import Bill


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
