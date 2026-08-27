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

    Pass ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD for a multi-day range
    instead of a single ?date= day (2026-08-27, per Shereena's Billing
    screen date picker needing a from/to range, same as the Bill History
    list). Both bounds are inclusive calendar dates.
    """

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request):
        search = request.query_params.get("search", "").strip() or None
        payment_method = request.query_params.get("payment_method", "").strip().upper() or None

        date_from_param = request.query_params.get("date_from")
        date_to_param = request.query_params.get("date_to")
        if date_from_param and date_to_param:
            date_from = timezone.datetime.strptime(date_from_param, "%Y-%m-%d").date()
            date_to = timezone.datetime.strptime(date_to_param, "%Y-%m-%d").date()
            window_start = timezone.make_aware(timezone.datetime.combine(date_from, timezone.datetime.min.time()))
            window_end = timezone.make_aware(timezone.datetime.combine(date_to, timezone.datetime.min.time())) + timezone.timedelta(days=1)
            report = services.daily_collections(
                request.tenant, window_start=window_start, window_end=window_end,
                search=search, payment_method=payment_method,
            )
        else:
            date_param = request.query_params.get("date")
            date = timezone.datetime.strptime(date_param, "%Y-%m-%d").date() if date_param else timezone.localdate()
            report = services.daily_collections(request.tenant, date, search=search, payment_method=payment_method)
        return Response(DailyCollectionsSerializer(report).data)


class MySalesView(APIView):
    """'My Sales' (Shereena, 2026-08-25) — everything Daily Collections
    above has (payment breakdown w/ percentages, peak hour, avg/largest/
    smallest bill, searchable+payment-method-filterable bill list with
    item_count per bill), scoped to just the calling cashier's own
    processed bills instead of the whole restaurant's. One endpoint covers
    what would otherwise be 3 (summary, list+filter, search).

    Scoped to the cashier's current OPEN SHIFT (opened_at -> now), not the
    calendar day (2026-08-25 correction, per Shereena — a shift can start
    mid-afternoon and run past midnight, so "today" and "this shift" aren't
    the same window; Cash Reconciliation already uses the shift window the
    same way, see shift_totals_by_method). Falls back to the calendar day
    only if the cashier has no shift open right now.
    """

    permission_classes = [IsCashierOrManager, IsBillingEnabled]

    def get(self, request):
        search = request.query_params.get("search", "").strip() or None
        payment_method = request.query_params.get("payment_method", "").strip().upper() or None
        shift = services.get_current_shift(request.user)
        if shift is not None:
            report = services.daily_collections(
                request.tenant, window_start=shift.opened_at, window_end=timezone.now(),
                search=search, payment_method=payment_method, cashier=request.user,
            )
        else:
            report = services.daily_collections(
                request.tenant, search=search, payment_method=payment_method, cashier=request.user,
            )
        return Response(DailyCollectionsSerializer(report).data)
