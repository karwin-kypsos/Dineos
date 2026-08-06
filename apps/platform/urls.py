from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ActivityLogListView,
    DashboardView,
    EndImpersonationView,
    ImpersonateView,
    PlatformLoginView,
    TeamViewSet,
    TenantViewSet,
    VerifyPlatformLoginView,
)

router = DefaultRouter()
router.register("tenants", TenantViewSet, basename="platform-tenant")
router.register("team", TeamViewSet, basename="platform-team")

urlpatterns = [
    path("auth/login/", PlatformLoginView.as_view(), name="platform-login"),
    path("auth/verify-2fa/", VerifyPlatformLoginView.as_view(), name="platform-verify-2fa"),
    path("dashboard/", DashboardView.as_view(), name="platform-dashboard"),
    path("activity/", ActivityLogListView.as_view(), name="platform-activity"),
    path("tenants/<int:restaurant_id>/impersonate/", ImpersonateView.as_view(), name="platform-impersonate"),
    path("impersonation-sessions/<uuid:session_id>/end/", EndImpersonationView.as_view(), name="platform-impersonation-end"),
    path("", include(router.urls)),
]
