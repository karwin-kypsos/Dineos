import uuid

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

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
    to one day for the Cashier Home 'today's bills' list.
    """

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request):
        date_param = request.query_params.get("date", "").strip()
        date = None
        if date_param == "today":
            date = timezone.localdate()
        elif date_param:
            date = parse_date(date_param)

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
        return Response(services.get_bill_preview(session_id))


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


class PayBillView(APIView):
    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def post(self, request):
        serializer = PayBillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if get_tenant_from_session(serializer.validated_data["session_id"]) != request.tenant:
            raise PermissionDenied("This session does not belong to your restaurant.")
        bill = services.pay_bill(
            serializer.validated_data["session_id"],
            serializer.validated_data["payment_method"],
            request.user,
            amount_received=serializer.validated_data.get("amount_received"),
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
        return Response(services.get_takeaway_bill_preview(order_id))


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

        bill = services.pay_takeaway_bill(
            order_id, serializer.validated_data["payment_method"], request.user,
            amount_received=serializer.validated_data.get("amount_received"),
        )
        return Response(BillSerializer(bill).data, status=201)
