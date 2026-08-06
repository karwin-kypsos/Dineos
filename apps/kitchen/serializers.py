from rest_framework import serializers

from .models import KDSDevice


class KDSDeviceSerializer(serializers.ModelSerializer):
    """Admin's Kitchen Devices screen — api_key is shown in full (not
    masked) because it's the one moment the Admin can read it off to
    configure the physical tablet; there's no other channel to deliver
    it (see apps.authentication.management.commands.seed_demo for why
    that's also true of the original demo device)."""

    class Meta:
        model = KDSDevice
        fields = ["id", "branch", "label", "api_key", "is_active", "last_seen_at", "created_at"]
        read_only_fields = ["id", "api_key", "last_seen_at", "created_at"]
