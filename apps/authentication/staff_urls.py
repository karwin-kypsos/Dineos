from rest_framework.routers import DefaultRouter

from .views import StaffViewSet

router = DefaultRouter()
router.register("", StaffViewSet, basename="staff")

urlpatterns = router.urls
