from rest_framework import viewsets

from core.permissions import IsAdmin

from .models import KDSDevice
from .serializers import KDSDeviceSerializer


class KDSDeviceViewSet(viewsets.ModelViewSet):
    """Admin's Kitchen Devices screen — register/view the tablets running
    the Kitchen Display Screen and read off their api_key to configure
    one. No delete (matches the deactivate-not-delete pattern used
    elsewhere): losing a device row would orphan Order.notifications/
    activity tied to it.
    """

    serializer_class = KDSDeviceSerializer
    permission_classes = [IsAdmin]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        return KDSDevice.objects.filter(restaurant=self.request.user.restaurant)

    def perform_create(self, serializer):
        serializer.save(restaurant=self.request.user.restaurant)
