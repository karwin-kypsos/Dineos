import uuid

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tables.models import TableSession
from core.permissions import FeatureEnabledPermission, IsAnyStaff
from core.tenancy import get_tenant_from_session

from . import services
from .models import Bill
from .serializers import BillSerializer, PayBillSerializer, PayTakeawayBillSerializer

IsBillingEnabled = FeatureEnabledPermission("billing_enabled")


class BillListView(APIView):
    """Cashier's bill history/reconciliation screen — every bill across
    every cashier, both dine-in and takeaway, searchable and filterable.
    No date filter by default (so ?cashier=<id> alone returns that
    cashier's full history); pass ?date=today or ?date=YYYY-MM-DD to scope
    to one day for the Cashier Home 'today's bills' list, or
    ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD for an inclusive date range
    (2026-08-26, per Hari's request for a from/to bill history filter).
    """

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request):
        date_param = request.query_params.get("date", "").strip()
        date = None
        if date_param == "today":
            date = timezone.localdate()
        elif date_param:
            date = parse_date(date_param)

        date_from_param = request.query_params.get("date_from", "").strip()
        date_to_param = request.query_params.get("date_to", "").strip()
        date_from = parse_date(date_from_param) if date_from_param else None
        date_to = parse_date(date_to_param) if date_to_param else None

        payment_method = request.query_params.get("payment_method", "").strip().upper()
        if payment_method not in Bill.PaymentMethod.values:
            payment_method = None

        branch_id = request.query_params.get("branch")
        if branch_id:
            try:
                uuid.UUID(branch_id)
            except ValueError:
                branch_id = None  # malformed branch id — no filter applied, same convention as StaffViewSet

        bills = services.list_bills(
            request.tenant,
            date=date,
            date_from=date_from,
            date_to=date_to,
            payment_method=payment_method,
            cashier_id=request.query_params.get("cashier") or None,
            branch=branch_id,
            search=request.query_params.get("search", "").strip() or None,
        )
        return Response(BillSerializer(bills, many=True).data)


class SessionBillView(APIView):
    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request, session_id):
        if get_tenant_from_session(session_id) != request.tenant:
            raise PermissionDenied("This session does not belong to your restaurant.")
        existing = Bill.objects.filter(session_id=session_id).first()
        if existing:
            return Response(BillSerializer(existing).data)
        return Response(services.preview_for_response(services.get_bill_preview(session_id)))


class BillDetailView(APIView):
    """Receipt screen — re-fetch any past bill (dine-in or takeaway) by the
    Bill's own id, any time after payment (reprint, navigate back to it,
    view from a bill list, etc.) — distinct from Bill Preview/Takeaway Bill
    Preview above, which key off session_id/order_id and only work before
    a Bill exists yet.
    """

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request, bill_id):
        bill = services.restaurant_bills_qs(request.tenant).filter(id=bill_id).first()
        if bill is None:
            return Response({"detail": "Not found."}, status=404)
        return Response(BillSerializer(bill).data)


def _reject_other_branch(request, branch):
    """A branch-assigned user may only bill their OWN branch.

    2026-09-24. The guard here was restaurant-level only, so a Cashier at
    Indiranagar could close a Koramangala table's bill - proven live: the
    Bill was created with branch=Koramangala while processed_by was the
    Indiranagar cashier, which puts another branch's revenue into this
    cashier's shift and reconciliation. Table/Menu/Inventory listing was
    already made strict this way on 2026-09-14 and 2026-09-21; billing was
    the one mutation the rule never reached. An Admin has no branch of
    their own and still sees the whole restaurant, same as everywhere else.
    """
    user_branch_id = getattr(request.user, "branch_id", None)
    if user_branch_id is not None and branch is not None and branch.id != user_branch_id:
        raise PermissionDenied("This bill belongs to a different branch.")


class PayBillView(APIView):
    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def post(self, request):
        serializer = PayBillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if get_tenant_from_session(serializer.validated_data["session_id"]) != request.tenant:
            raise PermissionDenied("This session does not belong to your restaurant.")

        session = TableSession.objects.select_related("table__branch").filter(
            id=serializer.validated_data["session_id"]
        ).first()
        if session is not None:
            _reject_other_branch(request, session.table.branch)

        try:
            bill = services.pay_bill(
                serializer.validated_data["session_id"],
                serializer.validated_data["payment_method"],
                request.user,
                amount_received=serializer.validated_data.get("amount_received"),
            )
        except services.UnderPaymentError as error:
            return Response(
                {
                    "detail": "Amount received is less than the bill total.",
                    "total_amount": str(error.total_amount),
                    "amount_received": str(error.amount_received),
                    "shortfall": str(error.shortfall),
                },
                status=400,
            )
        return Response(BillSerializer(bill).data, status=201)


class TakeawayBillView(APIView):
    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request, order_id):
        from apps.orders.models import Order

        order = Order.objects.filter(id=order_id).select_related("branch__restaurant").first()
        if order is None or order.branch is None or order.branch.restaurant_id != request.tenant.id:
            raise PermissionDenied("This order does not belong to your restaurant.")

        existing = Bill.objects.filter(order_id=order_id).first()
        if existing:
            return Response(BillSerializer(existing).data)
        return Response(services.preview_for_response(services.get_takeaway_bill_preview(order_id)))


class PayTakeawayBillView(APIView):
    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def post(self, request):
        from apps.orders.models import Order

        serializer = PayTakeawayBillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order_id = serializer.validated_data["order_id"]

        order = Order.objects.filter(id=order_id).select_related("branch__restaurant").first()
        if order is None or order.branch is None or order.branch.restaurant_id != request.tenant.id:
            raise PermissionDenied("This order does not belong to your restaurant.")
        _reject_other_branch(request, order.branch)

        try:
            bill = services.pay_takeaway_bill(
                order_id, serializer.validated_data["payment_method"], request.user,
                amount_received=serializer.validated_data.get("amount_received"),
            )
        except services.UnderPaymentError as error:
            return Response(
                {
                    "detail": "Amount received is less than the bill total.",
                    "total_amount": str(error.total_amount),
                    "amount_received": str(error.amount_received),
                    "shortfall": str(error.shortfall),
                },
                status=400,
            )
        return Response(BillSerializer(bill).data, status=201)
