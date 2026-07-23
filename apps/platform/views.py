from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.restaurant.models import Restaurant
from core.permissions import IsPlatformAdmin

from .authentication import PlatformJWTAuthentication
from .serializers import PlatformLoginSerializer, RestaurantSerializer, issue_platform_access_token


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
        serializer.save(**extra)
