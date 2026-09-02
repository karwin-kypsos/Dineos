from rest_framework import serializers

from .models import Feedback


class FeedbackSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = Feedback
        fields = ["id", "bill", "branch", "rating", "comment", "created_at", "items"]
        read_only_fields = fields

    def get_items(self, obj):
        """The dishes the rating/comment is actually about (2026-09-02, per
        Karwin's report - a comment alone gave no way to tell which food
        item a customer meant). Same order-resolution logic as
        BillSerializer.get_items - all rounds for a takeaway bill, all
        non-cancelled orders for a dine-in session."""
        from apps.billing import services as billing_services
        from apps.orders.services import takeaway_group

        bill = obj.bill
        orders = (
            takeaway_group(bill.order)
            if bill.order_id
            else bill.session.orders.exclude(status="CANCELLED").prefetch_related("items").order_by("round_number")
        )
        return billing_services.line_items(orders)


class FeedbackCreateSerializer(serializers.Serializer):
    bill_id = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=140, required=False, allow_blank=True, default="")
