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
    payment_status = serializers.SerializerMethodField()
    rounds = serializers.SerializerMethodField()
    rounds_count = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()

    def get_table_number(self, obj):
        return obj.session.table.table_number if obj.session_id else None

    def _orders(self, obj):
        # A takeaway bill's order is always the root of its group (see
        # services.pay_takeaway_bill) — pull every round's orders in, not
        # just the root's, so a multi-round takeaway receipt isn't missing
        # whatever was added in round 2/3/etc. Cached per-instance since
        # get_items/get_rounds/get_rounds_count/get_items_count/get_timeline
        # all need the same list.
        if not hasattr(obj, "_bill_orders_cache"):
            from apps.orders.services import takeaway_group

            obj._bill_orders_cache = list(
                takeaway_group(obj.order)
                if obj.order_id
                else obj.session.orders.exclude(status="CANCELLED").prefetch_related("items").order_by("round_number")
            )
        return obj._bill_orders_cache

    def get_items(self, obj):
        from . import services

        return services.line_items(self._orders(obj))

    def get_rounds(self, obj):
        from . import services

        return [
            {
                "round_number": order.round_number,
                "placed_at": order.placed_at,
                "items": services.line_items([order]),
            }
            for order in self._orders(obj)
        ]

    def get_rounds_count(self, obj):
        return len(self._orders(obj))

    def get_items_count(self, obj):
        return sum(item.quantity for order in self._orders(obj) for item in order.items.all())

    def get_timeline(self, obj):
        # "Transaction Timeline" (2026-08-25, per Shereena's Bill Detail
        # mockup) — reconstructed from timestamps every order/session/bill
        # already tracks (accepted_at/preparing_at/ready_at/collected_at/
        # served_at per round, session.opened_at/bill_requested_at, and
        # Bill.paid_at itself), not a separate event log.
        events = []
        if obj.session_id:
            events.append({"label": f"Customer seated at Table {obj.session.table.table_number}", "timestamp": obj.session.opened_at})
        for order in self._orders(obj):
            events.append({"label": f"Round {order.round_number} placed — KOT sent to kitchen", "timestamp": order.placed_at})
            if order.accepted_at:
                events.append({"label": f"Round {order.round_number}: kitchen accepted order", "timestamp": order.accepted_at})
            if order.ready_at:
                events.append({"label": f"Round {order.round_number} food ready", "timestamp": order.ready_at})
            if order.collected_at:
                events.append({"label": f"Round {order.round_number} collected by server", "timestamp": order.collected_at})
            if order.served_at:
                events.append({"label": f"Round {order.round_number} served to table", "timestamp": order.served_at})
        if obj.session_id and obj.session.bill_requested_at:
            events.append({"label": "Bill requested by customer", "timestamp": obj.session.bill_requested_at})
        events.append({"label": "Bill paid", "timestamp": obj.paid_at})
        events.sort(key=lambda e: e["timestamp"])
        return events

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

    def get_payment_status(self, obj):
        # A Bill row existing at all means it's paid — see SessionBillView/
        # TakeawayBillView, which only ever return this serializer once
        # payment has actually happened (a not-yet-paid session/order goes
        # through get_bill_preview/get_takeaway_bill_preview instead).
        return "PAID"

    class Meta:
        model = Bill
        fields = [
            "id",
            "session",
            "order",
            "branch",
            "payment_status",
            "table_number",
            "restaurant_name",
            "branch_name",
            "branch_address",
            "branch_phone",
            "gst_percentage",
            "service_charge_percentage",
            "items",
            "rounds",
            "rounds_count",
            "items_count",
            "timeline",
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
    item_count = serializers.IntegerField()
    elapsed_seconds = serializers.IntegerField()
    elapsed_formatted = serializers.CharField()


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
    cash_percentage = serializers.FloatField()
    card_percentage = serializers.FloatField()
    upi_percentage = serializers.FloatField()
    tables_served = serializers.IntegerField()
    cashier_name = serializers.CharField()
    status = serializers.ChoiceField(choices=["OPEN", "CLOSED"])
    counted_cash = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)
    discrepancy_amount = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)
    is_matched = serializers.BooleanField(allow_null=True)
    closed_at = serializers.DateTimeField(allow_null=True)


class CloseShiftRequestSerializer(serializers.Serializer):
    counted_cash = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))
    acknowledge_discrepancy = serializers.BooleanField(default=False)
    discrepancy_reason = serializers.CharField(required=False, allow_blank=True, default="")


class DailyBillSerializer(serializers.ModelSerializer):
    table_number = serializers.CharField(source="session.table.table_number", read_only=True)
    item_count = serializers.SerializerMethodField()

    def get_item_count(self, obj):
        # "4 items" / "5 items" per row on My Sales' Today's Bills list
        # (2026-08-25, per Shereena) — total quantity, every round included
        # for dine-in (session.orders), just the one order for takeaway.
        from apps.orders.services import takeaway_group

        orders = takeaway_group(obj.order) if obj.order_id else obj.session.orders.exclude(status="CANCELLED")
        return sum(item.quantity for order in orders for item in order.items.all())

    class Meta:
        model = Bill
        fields = ["id", "table_number", "paid_at", "payment_method", "total_amount", "item_count"]
        read_only_fields = fields


class PaymentBreakdownSerializer(serializers.Serializer):
    cash = serializers.DecimalField(max_digits=10, decimal_places=2)
    card = serializers.DecimalField(max_digits=10, decimal_places=2)
    upi = serializers.DecimalField(max_digits=10, decimal_places=2)
    cash_percentage = serializers.FloatField()
    card_percentage = serializers.FloatField()
    upi_percentage = serializers.FloatField()


class CashierCollectionSerializer(serializers.Serializer):
    shift_id = serializers.UUIDField()
    cashier_id = serializers.UUIDField()
    cashier_name = serializers.CharField()
    tables_served = serializers.IntegerField()
    total_collected = serializers.DecimalField(max_digits=10, decimal_places=2)
    status = serializers.ChoiceField(choices=["NOT_SUBMITTED", "MATCHED", "DISCREPANCY"])
    opened_at = serializers.DateTimeField()
    closed_at = serializers.DateTimeField(allow_null=True)


class DailyCollectionsSerializer(serializers.Serializer):
    date = serializers.DateField()
    total_collected = serializers.DecimalField(max_digits=10, decimal_places=2)
    vs_yesterday = serializers.DecimalField(max_digits=10, decimal_places=2)
    vs_yesterday_percentage = serializers.FloatField(allow_null=True)
    vs_last_week = serializers.DecimalField(max_digits=10, decimal_places=2)
    vs_last_week_percentage = serializers.FloatField(allow_null=True)
    tables_served = serializers.IntegerField()
    tables_count = serializers.IntegerField()
    avg_bill_value = serializers.DecimalField(max_digits=10, decimal_places=2)
    largest_bill = serializers.DecimalField(max_digits=10, decimal_places=2)
    smallest_bill = serializers.DecimalField(max_digits=10, decimal_places=2)
    peak_hour = serializers.CharField(allow_null=True)
    payment_breakdown = PaymentBreakdownSerializer()
    bills = DailyBillSerializer(many=True)
