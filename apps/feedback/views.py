from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Bill
from core.permissions import IsAnyStaff

from .models import Feedback
from .serializers import FeedbackCreateSerializer, FeedbackSerializer


def _restaurant_for_bill(bill):
    # Same resolution path as apps.billing.services.restaurant_bills_qs —
    # a Bill is scoped to a restaurant either via its session (dine-in) or
    # its order (takeaway), never both (see Bill's "exactly one of session
    # or order" constraint).
    return bill.session.table.restaurant if bill.session_id else bill.order.branch.restaurant


class SubmitFeedbackView(APIView):
    """Customer's 'Rate your experience' screen — no auth, submitted right
    after payment. Idempotent per bill: resubmitting (e.g. a flaky
    connection retry) returns the existing feedback rather than erroring
    or creating a duplicate, same convention as pay_bill.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = FeedbackCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        bill = (
            Bill.objects.select_related("branch", "session__table__restaurant", "order__branch__restaurant")
            .filter(id=data["bill_id"])
            .first()
        )
        if bill is None:
            return Response({"bill_id": "Bill not found."}, status=status.HTTP_404_NOT_FOUND)

        existing = Feedback.objects.filter(bill=bill).first()
        if existing is not None:
            return Response(FeedbackSerializer(existing).data, status=status.HTTP_200_OK)

        feedback = Feedback.objects.create(
            restaurant=_restaurant_for_bill(bill),
            branch=bill.branch,
            bill=bill,
            rating=data["rating"],
            comment=data["comment"],
        )
        return Response(FeedbackSerializer(feedback).data, status=status.HTTP_201_CREATED)


class FeedbackListView(APIView):
    """Admin/Manager screen for reviewing customer feedback."""

    permission_classes = [IsAnyStaff]

    def get(self, request):
        feedback = (
            Feedback.objects.filter(restaurant=request.tenant)
            .select_related("bill", "branch")
            .order_by("-created_at")
        )

        branch_id = request.query_params.get("branch")
        if branch_id:
            feedback = feedback.filter(branch_id=branch_id)

        rating_param = request.query_params.get("rating", "").strip()
        if rating_param.isdigit() and 1 <= int(rating_param) <= 5:
            feedback = feedback.filter(rating=int(rating_param))

        return Response(FeedbackSerializer(feedback, many=True).data)
