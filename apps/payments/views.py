import json
import logging

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.billing import services as billing_services
from apps.billing.models import Bill
from core.permissions import FeatureEnabledPermission, IsAnyStaff
from core.razorpay_client import RazorpayUnavailableError, create_order, create_qr_code, verify_webhook_signature
from core.tenancy import get_tenant_from_session

from .models import PaymentAttempt
from .serializers import (
    CreateCustomerRazorpayOrderSerializer,
    CreateRazorpayOrderSerializer,
    CreateRazorpayQrCodeSerializer,
    PaymentAttemptSerializer,
)

# Razorpay's own reported payment method at webhook time (authoritative —
# see RazorpayWebhookView._resolve_payment_method) maps onto our two-value
# enum. Checkout is configured (frontend-side) to only ever offer card/upi,
# so this covers every case in practice; anything else falls back safely.
_RAZORPAY_METHOD_MAP = {"card": "CARD", "emi": "CARD", "upi": "UPI"}

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


class CreateRazorpayQrCodeView(APIView):
    """Cashier's 'Pay by QR' action (2026-09-09, per Shereena) — a separate
    button from 'Pay via Razorpay' (CreateRazorpayOrderView above), since
    Razorpay QR Codes are a distinct product from Orders/Checkout: this
    returns a ready-to-render QR image directly, rather than a Checkout
    widget the customer has to type into. Single-use + fixed-amount (see
    core.razorpay_client.create_qr_code), so it can't be reused for a
    different bill. Always UPI — a QR is inherently scan-to-pay, there's no
    "pick Card instead" the way Checkout offers. Same PaymentAttempt model,
    same webhook (RazorpayWebhookView handles both payment.captured and
    qr_code.credited), same everything downstream as the Checkout flow —
    only how the QR is presented to the payer differs.
    """

    permission_classes = [IsAnyStaff, IsBillingEnabled]

    def post(self, request):
        serializer = CreateRazorpayQrCodeSerializer(data=request.data)
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
            receipt_id = session_id
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
            receipt_id = order_id

        amount = preview["total_amount"]
        try:
            qr_code = create_qr_code(amount, name=f"DineOS bill {receipt_id}")
        except RazorpayUnavailableError as e:
            return Response({"detail": str(e)}, status=502)

        attempt = PaymentAttempt.objects.create(
            restaurant=request.tenant,
            razorpay_qr_code_id=qr_code["id"],
            payment_method=PaymentAttempt.PaymentMethod.UPI,
            amount=amount,
            initiated_by=request.user,
            **attempt_kwargs,
        )
        return Response(
            {
                "payment_attempt": PaymentAttemptSerializer(attempt).data,
                "razorpay_qr_code_id": qr_code["id"],
                "image_url": qr_code["image_url"],
                "amount": amount,
                "currency": "INR",
            },
            status=201,
        )


class CreateCustomerRazorpayOrderView(APIView):
    """Customer self-checkout (2026-09-09) — the no-auth counterpart to
    CreateRazorpayOrderView, called directly from the customer's own phone
    (the QR-ordering flow's bill screen), no cashier device involved. Same
    trust model already used by every other customer-facing endpoint in
    this codebase (CreateOrderView, TableViewSet.session/bill_request,
    OrdersBySessionView): the TableSession's own unguessable UUID is the
    only "auth" — there is no new security precedent here. Dine-in only;
    there is no customer-facing takeaway flow to hang a takeaway version of
    this off of. Reuses the exact same PaymentAttempt model and
    RazorpayWebhookView as the cashier flow — nothing about payment
    confirmation differs based on who initiated it.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "razorpay_customer_order"

    def post(self, request):
        serializer = CreateCustomerRazorpayOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session_id = serializer.validated_data["session_id"]

        restaurant = get_tenant_from_session(session_id)
        if restaurant is None:
            return Response({"detail": "Session not found."}, status=404)
        if Bill.objects.filter(session_id=session_id).exists():
            return Response({"detail": "This bill has already been paid."}, status=409)

        preview = billing_services.get_bill_preview(session_id)
        amount = preview["total_amount"]
        try:
            razorpay_order = create_order(
                amount, receipt=str(session_id), linked_account_id=restaurant.razorpay_account_id or None,
            )
        except RazorpayUnavailableError as e:
            return Response({"detail": str(e)}, status=502)

        PaymentAttempt.objects.create(
            session_id=session_id,
            restaurant=restaurant,
            razorpay_order_id=razorpay_order["id"],
            # Placeholder — Checkout lets the customer pick Card or UPI
            # itself, so this is corrected from Razorpay's own reported
            # method at webhook time (_resolve_payment_method below), not
            # trusted as-is the way a cashier's pre-selection currently is.
            payment_method=PaymentAttempt.PaymentMethod.UPI,
            amount=amount,
            initiated_by=None,
        )
        return Response(
            {"razorpay_order_id": razorpay_order["id"], "amount": amount, "currency": "INR",
             "key_id": settings.RAZORPAY_KEY_ID},
            status=201,
        )


class RazorpayWebhookView(APIView):
    """Receives Razorpay's payment.captured (Checkout/Orders) and
    qr_code.credited (2026-09-09, per Shereena's 'Pay by QR' — a distinct
    Razorpay product/event, see CreateRazorpayQrCodeView) events, verifies
    the signature, and — on first successful verification for a given
    order/QR code — hands off to the SAME
    apps.billing.services.pay_bill/pay_takeaway_bill the cashier's manual
    CASH/CARD/UPI flow already uses, so a gateway-confirmed payment gets
    the identical Bill row, session close, WebSocket broadcast, and
    Admin/Manager notification a manual payment gets, with no duplicated
    logic — one shared confirmation path regardless of which of the three
    ways (Checkout via cashier, Checkout via customer, or QR) the payment
    was initiated. No staff auth — Razorpay calls this directly — so the
    request is trusted only once its signature verifies against
    RAZORPAY_WEBHOOK_SECRET.
    """

    permission_classes = [AllowAny]

    def _resolve_payment_method(self, attempt, payment_entity):
        # 2026-09-09 correctness fix: attempt.payment_method is only ever a
        # pre-selection (a cashier's manual choice, or the customer
        # self-checkout placeholder) — Checkout itself may let the payer
        # pick a different method than what was pre-selected. Razorpay's
        # own reported method on the captured payment is authoritative;
        # fall back to the pre-selection only for a method we don't
        # recognize (never crash the webhook over an unmapped value).
        razorpay_method = payment_entity.get("method")
        return _RAZORPAY_METHOD_MAP.get(razorpay_method, attempt.payment_method)

    def post(self, request):
        signature = request.headers.get("X-Razorpay-Signature", "")
        raw_body = request.body

        try:
            verify_webhook_signature(raw_body.decode("utf-8"), signature)
        except RazorpayUnavailableError as e:
            logger.warning("Razorpay webhook rejected: %s", e)
            return Response({"detail": "Invalid signature."}, status=400)

        payload = json.loads(raw_body.decode("utf-8"))
        event = payload.get("event")
        payment_entity = payload["payload"]["payment"]["entity"]

        if event == "payment.captured":
            attempt = get_object_or_404(PaymentAttempt, razorpay_order_id=payment_entity["order_id"])
        elif event == "qr_code.credited":
            qr_code_id = payload["payload"]["qr_code"]["entity"]["id"]
            attempt = get_object_or_404(PaymentAttempt, razorpay_qr_code_id=qr_code_id)
        else:
            return Response({"detail": "Event ignored."}, status=200)

        if attempt.status == PaymentAttempt.Status.PAID:
            return Response({"detail": "Already processed."}, status=200)  # idempotent replay

        payment_method = self._resolve_payment_method(attempt, payment_entity)
        if attempt.session_id:
            bill = billing_services.pay_bill(attempt.session_id, payment_method, attempt.initiated_by)
        else:
            bill = billing_services.pay_takeaway_bill(attempt.order_id, payment_method, attempt.initiated_by)

        attempt.status = PaymentAttempt.Status.PAID
        attempt.resolved_at = timezone.now()
        attempt.save(update_fields=["status", "resolved_at"])

        return Response({"detail": "Payment confirmed.", "bill_id": str(bill.id)}, status=200)
