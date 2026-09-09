import json
import logging

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing import services as billing_services
from apps.billing.models import Bill
from core.permissions import FeatureEnabledPermission, IsAnyStaff
from core.razorpay_client import RazorpayUnavailableError, create_order, verify_webhook_signature
from core.tenancy import get_tenant_from_session

from .models import PaymentAttempt
from .serializers import CreateRazorpayOrderSerializer, PaymentAttemptSerializer

logger = logging.getLogger(__name__)

IsBillingEnabled = FeatureEnabledPermission("billing_enabled")


class CreateRazorpayOrderView(APIView):
    """Cashier's 'Pay via Razorpay' action — creates a Razorpay Order and a
    matching PaymentAttempt row, but does NOT create a Bill yet: that only
    happens once RazorpayWebhookView confirms the payment actually went
    through, same as a cashier never getting a Bill for a payment they
    never actually collected. The existing manual CASH/CARD/UPI path
    (apps.billing.views.PayBillView/PayTakeawayBillView) is completely
    separate from this and untouched by it.

    Fund routing (2026-09-09): if this restaurant has linked a Razorpay
    account (Restaurant.razorpay_account_id, via Razorpay Route), the order
    routes straight to their own account. If not — which is every
    restaurant right now, since this platform's Razorpay account doesn't
    have Route enabled yet — the order is created plainly and settles to
    the platform's own account instead, so payment collection still works
    end to end today. See core.razorpay_client.create_order.
    """

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def post(self, request):
        serializer = CreateRazorpayOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "session_id" in data:
            session_id = data["session_id"]
            if get_tenant_from_session(session_id) != request.tenant:
                raise PermissionDenied("This session does not belong to your restaurant.")
            if Bill.objects.filter(session_id=session_id).exists():
                return Response({"detail": "This bill has already been paid."}, status=409)
            preview = billing_services.get_bill_preview(session_id)
            attempt_kwargs = {"session_id": session_id}
        else:
            from apps.orders.models import Order

            order_id = data["order_id"]
            order = Order.objects.filter(id=order_id).select_related("branch__restaurant").first()
            if order is None or order.branch is None or order.branch.restaurant_id != request.tenant.id:
                raise PermissionDenied("This order does not belong to your restaurant.")
            if Bill.objects.filter(order_id=order_id).exists():
                return Response({"detail": "This bill has already been paid."}, status=409)
            preview = billing_services.get_takeaway_bill_preview(order_id)
            attempt_kwargs = {"order_id": order_id}

        amount = preview["total_amount"]
        try:
            razorpay_order = create_order(
                amount, receipt=str(attempt_kwargs.get("session_id") or attempt_kwargs.get("order_id")),
                linked_account_id=request.tenant.razorpay_account_id or None,
            )
        except RazorpayUnavailableError as e:
            return Response({"detail": str(e)}, status=502)

        attempt = PaymentAttempt.objects.create(
            restaurant=request.tenant,
            razorpay_order_id=razorpay_order["id"],
            payment_method=data["payment_method"],
            amount=amount,
            initiated_by=request.user,
            **attempt_kwargs,
        )
        return Response(
            {
                "payment_attempt": PaymentAttemptSerializer(attempt).data,
                "razorpay_order_id": razorpay_order["id"],
                "amount": amount,
                "currency": "INR",
                "key_id": settings.RAZORPAY_KEY_ID,
            },
            status=201,
        )


class RazorpayWebhookView(APIView):
    """Receives Razorpay's payment.captured event, verifies its signature,
    and — on first successful verification for a given order — hands off to
    the SAME apps.billing.services.pay_bill/pay_takeaway_bill the cashier's
    manual CASH/CARD/UPI flow already uses, so a gateway-confirmed payment
    gets the identical Bill row, session close, WebSocket broadcast, and
    Admin/Manager notification a manual payment gets, with no duplicated
    logic. No staff auth — Razorpay calls this directly — so the request is
    trusted only once its signature verifies against RAZORPAY_WEBHOOK_SECRET.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        signature = request.headers.get("X-Razorpay-Signature", "")
        raw_body = request.body

        try:
            verify_webhook_signature(raw_body.decode("utf-8"), signature)
        except RazorpayUnavailableError as e:
            logger.warning("Razorpay webhook rejected: %s", e)
            return Response({"detail": "Invalid signature."}, status=400)

        payload = json.loads(raw_body.decode("utf-8"))
        if payload.get("event") != "payment.captured":
            return Response({"detail": "Event ignored."}, status=200)

        razorpay_order_id = payload["payload"]["payment"]["entity"]["order_id"]
        attempt = get_object_or_404(PaymentAttempt, razorpay_order_id=razorpay_order_id)

        if attempt.status == PaymentAttempt.Status.PAID:
            return Response({"detail": "Already processed."}, status=200)  # idempotent replay

        if attempt.session_id:
            bill = billing_services.pay_bill(attempt.session_id, attempt.payment_method, attempt.initiated_by)
        else:
            bill = billing_services.pay_takeaway_bill(attempt.order_id, attempt.payment_method, attempt.initiated_by)

        attempt.status = PaymentAttempt.Status.PAID
        attempt.resolved_at = timezone.now()
        attempt.save(update_fields=["status", "resolved_at"])

        return Response({"detail": "Payment confirmed.", "bill_id": str(bill.id)}, status=200)
