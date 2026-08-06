from rest_framework.routers import DefaultRouter

from .views import KDSDeviceViewSet

router = DefaultRouter()
router.register("devices", KDSDeviceViewSet, basename="kds-device")

urlpatterns = router.urls
