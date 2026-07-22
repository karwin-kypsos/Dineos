from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAnyStaff

from . import services
from .models import Bill
from .serializers import BillSerializer, PayBillSerializer


class SessionBillView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request, session_id):
        existing = Bill.objects.filter(session_id=session_id).first()
        if existing:
            return Response(BillSerializer(existing).data)
        return Response(services.get_bill_preview(session_id))


class PayBillView(APIView):
    permission_classes = [IsAnyStaff]

    def post(self, request):
        serializer = PayBillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bill = services.pay_bill(
            serializer.validated_data["session_id"],
            serializer.validated_data["payment_method"],
            request.user,
        )
        return Response(BillSerializer(bill).data, status=201)
