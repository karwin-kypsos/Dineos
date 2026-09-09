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


class CreateCustomerRazorpayOrderSerializer(serializers.Serializer):
    """Customer self-checkout (2026-09-09) — dine-in only, no payment_method
    field: Razorpay Checkout itself presents Card/UPI to the customer inside
    its own widget, restricted to those two (see
    RazorpayWebhookView._resolve_payment_method), rather than the caller
    pre-selecting one the way a cashier does on CreateRazorpayOrderSerializer."""

    session_id = serializers.UUIDField()


class CreateRazorpayQrCodeSerializer(serializers.Serializer):
    """'Pay by QR' (2026-09-09, per Shereena) — a standalone QR button
    distinct from Checkout, always UPI (a QR is inherently scan-to-pay), so
    no payment_method field is needed the way CreateRazorpayOrderSerializer
    has one."""

    session_id = serializers.UUIDField(required=False)
    order_id = serializers.UUIDField(required=False)

    def validate(self, data):
        has_session = "session_id" in data
        has_order = "order_id" in data
        if has_session == has_order:
            raise serializers.ValidationError("Provide exactly one of session_id or order_id.")
        return data


class PaymentAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentAttempt
        fields = [
            "id", "razorpay_order_id", "razorpay_qr_code_id", "payment_method",
            "amount", "status", "created_at", "resolved_at",
        ]
        read_only_fields = fields
