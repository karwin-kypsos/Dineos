from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ActivityLogListView,
    DashboardView,
    PlatformLoginView,
    TeamViewSet,
    TenantViewSet,
)

router = DefaultRouter()
router.register("tenants", TenantViewSet, basename="platform-tenant")
router.register("team", TeamViewSet, basename="platform-team")

urlpatterns = [
    path("auth/login/", PlatformLoginView.as_view(), name="platform-login"),
    path("dashboard/", DashboardView.as_view(), name="platform-dashboard"),
    path("activity/", ActivityLogListView.as_view(), name="platform-activity"),
    path("", include(router.urls)),
]
