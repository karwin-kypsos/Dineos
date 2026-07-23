from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import PlatformLoginView, TenantViewSet

router = DefaultRouter()
router.register("tenants", TenantViewSet, basename="platform-tenant")

urlpatterns = [
    path("auth/login/", PlatformLoginView.as_view(), name="platform-login"),
    path("", include(router.urls)),
]
