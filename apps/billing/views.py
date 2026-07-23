from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import FeatureEnabledPermission, IsAnyStaff
from core.tenancy import get_tenant_from_session

from . import services
from .models import Bill
from .serializers import BillSerializer, PayBillSerializer

IsBillingEnabled = FeatureEnabledPermission("billing_enabled")


class SessionBillView(APIView):
    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def get(self, request, session_id):
        if get_tenant_from_session(session_id) != request.tenant:
            raise PermissionDenied("This session does not belong to your restaurant.")
        existing = Bill.objects.filter(session_id=session_id).first()
        if existing:
            return Response(BillSerializer(existing).data)
        return Response(services.get_bill_preview(session_id))


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
        )
        return Response(BillSerializer(bill).data, status=201)
