from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import FeatureEnabledPermission, IsAnyStaff, IsCashierOrManager

from . import services
from .models import CashierShift
from .serializers import (
    CashierDashboardSerializer,
    CashierShiftSerializer,
    CloseShiftRequestSerializer,
    DailyCollectionsSerializer,
    ShiftReconciliationSerializer,
)

IsBillingEnabled = FeatureEnabledPermission("billing_enabled")


def _get_owned_shift(request, shift_id):
    """A cashier can only ever touch their own shift; a Manager/Admin can
    open any shift within their own tenant (oversight/backup-cashier cases).
    """
    shift = get_object_or_404(CashierShift, id=shift_id, restaurant=request.tenant)
    if request.user.role == "CASHIER" and shift.cashier_id != request.user.id:
        raise PermissionDenied("This shift belongs to a different cashier.")
    return shift


class OpenShiftView(APIView):
    permission_classes = [IsCashierOrManager, IsBillingEnabled]

    def post(self, request):
        shift = services.open_shift(request.user)
        return Response(CashierShiftSerializer(shift).data, status=status.HTTP_201_CREATED)


class CurrentShiftView(APIView):
    """'Cashier Home' — the calling cashier's own dashboard."""

    permission_classes = [IsCashierOrManager, IsBillingEnabled]

    def get(self, request):
        dashboard = services.cashier_dashboard(request.tenant, request.user)
        return Response(CashierDashboardSerializer(dashboard).data)


class ShiftReconciliationView(APIView):
    permission_classes = [IsCashierOrManager, IsBillingEnabled]

    def get(self, request, shift_id):
        shift = _get_owned_shift(request, shift_id)
        totals = services.shift_totals_by_method(shift)
        return Response(ShiftReconciliationSerializer(totals).data)


class CloseShiftView(APIView):
    permission_classes = [IsCashierOrManager, IsBillingEnabled]

    def post(self, request, shift_id):
        shift = _get_owned_shift(request, shift_id)
        serializer = CloseShiftRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            closed = services.close_shift(
                shift,
                serializer.validated_data["counted_cash"],
                serializer.validated_data["acknowledge_discrepancy"],
                serializer.validated_data["discrepancy_reason"],
            )
        except services.ShiftAlreadyClosedError:
            return Response({"detail": "This shift is already closed."}, status=status.HTTP_409_CONFLICT)
        except services.DiscrepancyNotAcknowledgedError as error:
            return Response(
                {
                    "detail": "Counted cash does not match the system total.",
                    "discrepancy": str(error.discrepancy),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except services.DiscrepancyReasonRequiredError as error:
            return Response(
                {
                    "detail": "A reason is required to close with a discrepancy.",
                    "discrepancy": str(error.discrepancy),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(CashierShiftSerializer(closed).data)


class DailyCollectionsView(APIView):
    """'Daily Collections' — restaurant-wide, not scoped to one cashier's
    shift, so Admin/Manager/Cashier can all view it (matches the design's
    'Head Cashier' framing — this is oversight, not a personal shift view).
    """

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request):
        date_param = request.query_params.get("date")
        date = timezone.datetime.strptime(date_param, "%Y-%m-%d").date() if date_param else timezone.localdate()
        report = services.daily_collections(request.tenant, date)
        return Response(DailyCollectionsSerializer(report).data)
