from decimal import Decimal

from django.conf import settings
from django.db import models


class Restaurant(models.Model):
    name = models.CharField(max_length=255, default="My Restaurant")
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("5.00"))
    service_charge_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        db_table = "restaurant_settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        defaults = {
            "gst_percentage": Decimal(str(settings.DEFAULT_GST_PERCENTAGE)),
            "service_charge_percentage": Decimal(str(settings.DEFAULT_SERVICE_CHARGE_PERCENTAGE)),
        }
        obj, _ = cls.objects.get_or_create(pk=1, defaults=defaults)
        return obj

    def __str__(self):
        return self.name
