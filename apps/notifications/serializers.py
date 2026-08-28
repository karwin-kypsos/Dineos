from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    # Raw table_number (e.g. "5"), not just the table's id — Karwin (2026-08-28):
    # the notification list had no human-readable way to tell which table an
    # order/bill notification was about, same gap OrderSerializer.table_number
    # closed earlier for the orders list.
    table_number = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "type", "title", "body", "data", "order", "table", "table_number", "is_read", "created_at"]
        read_only_fields = fields

    def get_table_number(self, obj):
        return obj.table.table_number if obj.table_id else None
