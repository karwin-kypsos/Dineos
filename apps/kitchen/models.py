import secrets

from django.db import models


def _generate_api_key():
    return secrets.token_hex(32)


class KDSDevice(models.Model):
    restaurant = models.ForeignKey("restaurant.Restaurant", on_delete=models.CASCADE, related_name="kds_devices")
    branch = models.ForeignKey(
        "restaurant.Branch", on_delete=models.SET_NULL, null=True, blank=True, related_name="kds_devices"
    )
    label = models.CharField(max_length=255)
    api_key = models.CharField(max_length=64, unique=True, default=_generate_api_key)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "kds_devices"
        # 2026-09-21: this list is paginated (PAGE_SIZE 20, global), and an
        # unordered queryset lets Postgres return rows in any order per
        # query - so a device could appear on two pages or on neither.
        # Django was already warning about exactly this
        # (UnorderedObjectListWarning on the Kitchen Devices screen).
        ordering = ["label", "id"]

    def __str__(self):
        return self.label
