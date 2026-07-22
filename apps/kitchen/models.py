import secrets

from django.db import models


def _generate_api_key():
    return secrets.token_hex(32)


class KDSDevice(models.Model):
    label = models.CharField(max_length=255)
    api_key = models.CharField(max_length=64, unique=True, default=_generate_api_key)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "kds_devices"

    def __str__(self):
        return self.label
