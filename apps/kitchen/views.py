import uuid

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAdmin, IsKDSDevice

from .authentication import KDSKeyAuthentication
from .models import KDSDevice, _generate_api_key
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
        qs = KDSDevice.objects.filter(restaurant=self.request.user.restaurant)
        # Bug (2026-08-27, per Manikandan's testing): this had NO branch
        # filtering at all — every branch's Kitchen Devices screen showed
        # every OTHER branch's devices too. A KDS tablet always sits in one
        # specific kitchen/branch (unlike a shared MenuItem/Category, which
        # can legitimately apply to every branch), so this is a strict
        # match on ?branch=, not the Q(branch__isnull=True) fallback used
        # for genuinely shared resources elsewhere.
        branch_id = self.request.query_params.get("branch")
        if branch_id:
            try:
                uuid.UUID(branch_id)
                qs = qs.filter(branch_id=branch_id)
            except ValueError:
                pass  # malformed branch id — no filter applied, same convention as StaffViewSet/IngredientViewSet
        return qs

    def perform_create(self, serializer):
        serializer.save(restaurant=self.request.user.restaurant)

    @action(detail=True, methods=["post"], url_path="rotate-key")
    def rotate_key(self, request, pk=None):
        """Issue a fresh api_key for a lost/compromised tablet without
        deleting and recreating the device row (which would orphan its
        order/notification history). Old key stops working immediately."""
        device = self.get_object()
        device.api_key = _generate_api_key()
        device.save(update_fields=["api_key"])
        return Response(KDSDeviceSerializer(device).data)


class KDSDeviceMeView(APIView):
    """Lightweight self-check for the Kitchen app's first-time setup screen —
    confirms an api_key the operator just typed in resolves to a real, active
    device before it's saved on the tablet, without hitting a real KOT feed
    endpoint. Authenticating at all already refreshes last_seen_at
    (KDSKeyAuthentication does this on every request), so no separate
    heartbeat/ping endpoint is needed — this call itself is the heartbeat."""

    authentication_classes = [KDSKeyAuthentication]
    permission_classes = [IsKDSDevice]

    def get(self, request):
        return Response(KDSDeviceSerializer(request.auth).data)
