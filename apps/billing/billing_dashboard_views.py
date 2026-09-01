"""New /v1/billing/ endpoints (2026-08-27, per the Flutter team's Billing
API spec) — branch + date/date-range scoped summary, floor status,
payment split, cashier collections, cashier detail, and paginated
cashier bills. Thin wrappers reusing the same tested service functions
that already power the existing Billing dashboard endpoints
(GET /v1/cashier/collections/daily/ and /by-cashier/) rather than
duplicating their logic — just reshaped to the exact response format
this spec asked for, with branch filtering added throughout.
"""

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from core.pagination import SmallPageNumberPagination
from core.permissions import FeatureEnabledPermission, IsAnyStaff

from . import services
from .serializers import BillSerializer

IsBillingEnabled = FeatureEnabledPermission("billing_enabled")
User = get_user_model()


def _parse_date_params(request):
    date_param = request.query_params.get("date")
    from_param = request.query_params.get("from_date")
    to_param = request.query_params.get("to_date")
    date = timezone.datetime.strptime(date_param, "%Y-%m-%d").date() if date_param else None
    date_from = timezone.datetime.strptime(from_param, "%Y-%m-%d").date() if from_param else None
    date_to = timezone.datetime.strptime(to_param, "%Y-%m-%d").date() if to_param else None
    return date, date_from, date_to


def _branch_param(request):
    """2026-09-01, per Karwin's report - a Manager is always pinned to
    exactly one branch (unlike Admin), so requiring them to explicitly
    pass ?branch= on every one of these endpoints made no sense - without
    it, they'd silently get the cross-branch Admin view instead of just
    their own branch's numbers. Falls back to the caller's own branch when
    no explicit param is given; Admin has no fixed branch, so this stays a
    no-op (still optional, still cross-branch by default) for them."""
    return request.query_params.get("branch") or getattr(request.user, "branch_id", None)


def _collections_report(request, date, date_from, date_to, branch):
    """daily_collections() only accepts a single `date` OR a
    window_start/window_end pair (both required together), not
    date_from/date_to directly -- convert here, same convention as the
    existing DailyCollectionsView (a lone date_from or date_to without
    its other bound falls back to single-day mode, same as there)."""
    if date_from is not None and date_to is not None:
        window_start = timezone.make_aware(timezone.datetime.combine(date_from, timezone.datetime.min.time()))
        window_end = timezone.make_aware(timezone.datetime.combine(date_to, timezone.datetime.min.time())) + timezone.timedelta(days=1)
        return services.daily_collections(
            request.tenant, window_start=window_start, window_end=window_end, branch=branch,
        )
    return services.daily_collections(request.tenant, date=date, branch=branch)


class BillingSummaryView(APIView):
    """GET /v1/billing/summary/?branch=&date=|from_date&to_date="""

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request):
        date, date_from, date_to = _parse_date_params(request)
        branch = _branch_param(request)
        report = _collections_report(request, date, date_from, date_to, branch)
        return Response({
            "total_revenue": report["total_collected"],
            "total_orders": report["tables_served"],
            "avg_bill_amount": report["avg_bill_value"],
            "revenue_by_hour": report["revenue_by_hour"],
            "vs_yesterday": report["vs_yesterday"],
            "vs_yesterday_percentage": report["vs_yesterday_percentage"],
            "vs_last_week": report["vs_last_week"],
            "vs_last_week_percentage": report["vs_last_week_percentage"],
            "peak_hour": report["peak_hour"],
        })


class BillingFloorStatusView(APIView):
    """GET /v1/billing/floor-status/?branch=&date=|from_date&to_date="""

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request):
        date, date_from, date_to = _parse_date_params(request)
        branch = _branch_param(request)
        rows = services.floor_status(request.tenant, branch=branch, date=date, date_from=date_from, date_to=date_to)
        return Response(rows)


class BillingPaymentSplitView(APIView):
    """GET /v1/billing/payment-split/?branch=&date=|from_date&to_date="""

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request):
        date, date_from, date_to = _parse_date_params(request)
        branch = _branch_param(request)
        report = _collections_report(request, date, date_from, date_to, branch)
        pb = report["payment_breakdown"]
        return Response({
            "cash": {"amount": pb["cash"], "percentage": pb["cash_percentage"]},
            "card": {"amount": pb["card"], "percentage": pb["card_percentage"]},
            "upi": {"amount": pb["upi"], "percentage": pb["upi_percentage"]},
        })


class BillingCashiersView(APIView):
    """GET /v1/billing/cashiers/?branch=&date=|from_date&to_date="""

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request):
        date, date_from, date_to = _parse_date_params(request)
        branch = _branch_param(request)
        rows = services.cashier_collections(
            request.tenant, date=date, date_from=date_from, date_to=date_to, branch=branch,
        )
        status_labels = {"NOT_SUBMITTED": "Not submitted", "MATCHED": "Cash matched", "DISCREPANCY": "Difference"}
        return Response([
            {
                "user_id": row["cashier_id"],
                "user_name": row["cashier_name"],
                "role": "Cashier",
                "tables_served": row["tables_served"],
                "total_collected": row["total_collected"],
                "status": status_labels.get(row["status"], row["status"]),
                "shift_id": row["shift_id"],
                "opened_at": row["opened_at"],
                "closed_at": row["closed_at"],
                "expected_cash": row["expected_cash"],
                "counted_cash": row["counted_cash"],
                "discrepancy_amount": row["discrepancy_amount"],
                "discrepancy_reason": row["discrepancy_reason"],
            }
            for row in rows
        ])


class BillingCashierDetailView(APIView):
    """GET /v1/billing/cashiers/<cashier_id>/?branch=&date=|from_date&to_date="""

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request, cashier_id):
        cashier = get_object_or_404(User, id=cashier_id, restaurant=request.tenant)
        date, date_from, date_to = _parse_date_params(request)
        branch = _branch_param(request)
        detail = services.cashier_billing_detail(
            request.tenant, cashier, branch=branch, date=date, date_from=date_from, date_to=date_to,
        )
        return Response(detail)


class BillingCashierBillsView(ListAPIView):
    """GET /v1/billing/cashiers/<cashier_id>/bills/?branch=&date=|from_date&to_date=&page=&page_size=&search="""

    permission_classes = [IsAnyStaff, IsBillingEnabled]
    serializer_class = BillSerializer
    pagination_class = SmallPageNumberPagination

    def get_queryset(self):
        cashier_id = self.kwargs["cashier_id"]
        get_object_or_404(User, id=cashier_id, restaurant=self.request.tenant)
        date, date_from, date_to = _parse_date_params(self.request)
        branch = _branch_param(self.request)
        search = self.request.query_params.get("search", "").strip() or None
        return services.list_bills(
            self.request.tenant, date=date, date_from=date_from, date_to=date_to,
            cashier_id=cashier_id, branch=branch, search=search,
        )
