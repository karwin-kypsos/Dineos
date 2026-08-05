from rest_framework import status, viewsets
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication.models import User
from apps.restaurant.models import Restaurant
from core.permissions import IsPlatformAdmin

from .authentication import PlatformJWTAuthentication
from .models import PlatformActivityLog, PlatformAdmin
from .serializers import (
    PlatformActivityLogSerializer,
    PlatformAdminSerializer,
    PlatformLoginSerializer,
    RestaurantSerializer,
    issue_platform_access_token,
)


class PlatformLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PlatformLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = issue_platform_access_token(serializer.validated_data["admin"])
        return Response({"access": str(token)}, status=status.HTTP_200_OK)


class TenantViewSet(viewsets.ModelViewSet):
    """Super Admin's view of every client restaurant on the platform —
    create new tenants, and flip their per-add-on feature flags live.
    """

    queryset = Restaurant.objects.all()
    serializer_class = RestaurantSerializer
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def perform_create(self, serializer):
        # Platform-wide defaults (from .env) apply only when the Super Admin
        # doesn't explicitly set a rate for this specific tenant.
        from decimal import Decimal

        from django.conf import settings

        extra = {}
        if "gst_percentage" not in self.request.data:
            extra["gst_percentage"] = Decimal(str(settings.DEFAULT_GST_PERCENTAGE))
        if "service_charge_percentage" not in self.request.data:
            extra["service_charge_percentage"] = Decimal(str(settings.DEFAULT_SERVICE_CHARGE_PERCENTAGE))
        restaurant = serializer.save(**extra)
        PlatformActivityLog.objects.create(
            actor=self.request.user,
            action="TENANT_CREATED",
            restaurant=restaurant,
            description=f"Created tenant '{restaurant.name}' ({restaurant.slug})",
        )

    def perform_update(self, serializer):
        restaurant = serializer.save()
        PlatformActivityLog.objects.create(
            actor=self.request.user,
            action="TENANT_UPDATED",
            restaurant=restaurant,
            description=f"Updated tenant '{restaurant.name}' ({restaurant.slug})",
        )


class DashboardView(APIView):
    """Super Admin app's Dashboard screen — platform-wide summary numbers.
    Deliberately cheap aggregate queries only (counts, no joins across
    tenant data) since this runs on every dashboard load.
    """

    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        total_tenants = Restaurant.objects.count()
        active_tenants = Restaurant.objects.filter(is_active=True).count()
        total_staff = User.objects.count()
        recent_tenants = Restaurant.objects.order_by("-created_at")[:5]

        return Response(
            {
                "total_tenants": total_tenants,
                "active_tenants": active_tenants,
                "inactive_tenants": total_tenants - active_tenants,
                "total_staff_across_platform": total_staff,
                "recent_tenants": RestaurantSerializer(recent_tenants, many=True).data,
            }
        )


class ActivityLogListView(ListAPIView):
    """Super Admin app's Activity Log screen — paginated feed of platform
    actions, newest first (see PlatformActivityLog.Meta.ordering).
    """

    serializer_class = PlatformActivityLogSerializer
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    queryset = PlatformActivityLog.objects.select_related("actor", "restaurant").all()


class TeamViewSet(viewsets.ModelViewSet):
    """Super Admin app's Team screen — manage other platform admin accounts.
    No delete endpoint on purpose: matches the soft-deactivate pattern used
    by apps.authentication.StaffViewSet, so historical activity-log entries
    always resolve to a real actor.
    """

    queryset = PlatformAdmin.objects.all().order_by("-created_at")
    serializer_class = PlatformAdminSerializer
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):
        admin = serializer.save()
        PlatformActivityLog.objects.create(
            actor=self.request.user,
            action="TEAM_MEMBER_ADDED",
            description=f"Added team member '{admin.email}'",
        )

    def partial_update(self, request, *args, **kwargs):
        # Only is_active is meant to be toggled from this screen (deactivate
        # a departing team member) — everything else about an admin account
        # is set once at creation.
        instance = self.get_object()
        instance.is_active = request.data.get("is_active", instance.is_active)
        instance.save(update_fields=["is_active"])
        if not instance.is_active:
            PlatformActivityLog.objects.create(
                actor=request.user,
                action="TEAM_MEMBER_DEACTIVATED",
                description=f"Deactivated team member '{instance.email}'",
            )
        return Response(PlatformAdminSerializer(instance).data)
