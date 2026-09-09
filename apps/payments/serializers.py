from rest_framework import serializers

from .models import PaymentAttempt


class CreateRazorpayOrderSerializer(serializers.Serializer):
    session_id = serializers.UUIDField(required=False)
    order_id = serializers.UUIDField(required=False)
    payment_method = serializers.ChoiceField(choices=PaymentAttempt.PaymentMethod.choices)

    def validate(self, data):
        has_session = "session_id" in data
        has_order = "order_id" in data
        if has_session == has_order:
            raise serializers.ValidationError("Provide exactly one of session_id or order_id.")
        return data


class PaymentAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentAttempt
        fields = ["id", "razorpay_order_id", "payment_method", "amount", "status", "created_at", "resolved_at"]
        read_only_fields = fields
