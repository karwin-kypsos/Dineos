from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import KDSDeviceMeView, KDSDeviceViewSet

router = DefaultRouter()
router.register("devices", KDSDeviceViewSet, basename="kds-device")

urlpatterns = [
    path("devices/me/", KDSDeviceMeView.as_view(), name="kds-device-me"),
] + router.urls
