from rest_framework import serializers

from .models import Feedback


class FeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = Feedback
        fields = ["id", "bill", "branch", "rating", "comment", "created_at"]
        read_only_fields = fields


class FeedbackCreateSerializer(serializers.Serializer):
    bill_id = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=140, required=False, allow_blank=True, default="")
