"""Subscription endpoints for the restaurant's Admin (2026-10-05, per
Karwin). Razorpay checkout follows the same shape as
/v1/payments/razorpay/create-order/; see apps.subscriptions.services for the
rules."""
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.pagination import DineOSPageNumberPagination
from core.permissions import IsAdmin
from core.razorpay_client import RazorpayUnavailableError, verify_subscription_payment

from . import services
from .models import Subscription, SubscriptionPayment


def _money(value):
    return str(Decimal(value).quantize(Decimal("0.01"))) if value is not None else None


def _app_status(restaurant, sub):
    """The app's vocabulary: trialing / active / past_due / cancelled / none."""
    if sub is None:
        if restaurant.status == "TRIAL":
            return "trialing"
        # An unfinished checkout (CREATED) never counted as a subscription.
        last = restaurant.subscriptions.exclude(status=Subscription.Status.CREATED).order_by("-created_at").first()
        if last is None:
            return "none"
        return "past_due" if last.status == Subscription.Status.HALTED else "cancelled"
    if sub.status == Subscription.Status.PENDING:
        return "past_due"
    if sub.status == Subscription.Status.AUTHENTICATED and restaurant.status == "TRIAL":
        return "trialing"
    return "active"


def current_payload(restaurant):
    from apps.restaurant.models import Restaurant

    sub = services.current_subscription(restaurant)
    pending = services.pending_change(restaurant)
    tier = restaurant.plan_tier
    return {
        "plan_id": tier,
        "plan_name": Restaurant.PlanTier(tier).label,
        "status": _app_status(restaurant, sub),
        "organization_status": restaurant.status,
        "billing_cycle": "MONTHLY" if (sub or services.plan_price(tier)) else None,
        "amount": _money(sub.amount if sub else services.plan_price(tier)),
        "currency": "INR",
        "trial_ends_at": restaurant.trial_ends_at,
        "current_period_start": sub.current_start if sub else None,
        "current_period_end": sub.current_end if sub else None,
        # The next charge: the end of the current period, or a trial's end.
        "renews_at": None if (sub is None or sub.cancel_at_period_end) else (sub.current_end or sub.start_at),
        "cancel_at_period_end": bool(sub and sub.cancel_at_period_end),
        "pending_plan_id": pending.plan_tier if pending else None,
        "pending_change_at": (pending.start_at if pending else None),
        "subscription_id": str(sub.id) if sub else None,
        "razorpay_subscription_id": sub.razorpay_subscription_id if sub else None,
        "prices_set": all(services.plan_price(t) is not None for t in ("STARTER", "GROWTH", "ENTERPRISE")),
    }


class CurrentSubscriptionView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(current_payload(request.tenant))


class CheckoutSerializer(serializers.Serializer):
    plan_id = serializers.ChoiceField(choices=["STARTER", "GROWTH", "ENTERPRISE"])


class CheckoutView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tier = serializer.validated_data["plan_id"]
        try:
            sub, kind = services.start_checkout(request.tenant, tier, request.user)
        except services.PricesNotSet:
            return Response({"detail": "Prices for this plan aren't set yet.", "code": "prices_not_set"}, status=409)
        except services.AlreadyOnPlan:
            return Response({"detail": "You're already on this plan.", "code": "already_on_plan"}, status=409)
        except RazorpayUnavailableError as e:
            return Response({"detail": str(e)}, status=502)
        return Response(
            {
                "subscription_id": str(sub.id),
                "razorpay_subscription_id": sub.razorpay_subscription_id,
                "key_id": settings.RAZORPAY_KEY_ID,
                "plan_id": sub.plan_tier,
                # Decimal string for display, integer paise for Razorpay -
                # same pair as /v1/payments/razorpay/create-order/.
                "amount": _money(sub.amount),
                "amount_paise": int(sub.amount * 100),
                "currency": "INR",
                "billing_cycle": sub.billing_cycle,
                "change": kind,
                # When the first charge happens: null = at checkout.
                "first_charge_at": sub.start_at,
            },
            status=status.HTTP_201_CREATED,
        )


class VerifySerializer(serializers.Serializer):
    razorpay_subscription_id = serializers.CharField(max_length=64)
    razorpay_payment_id = serializers.CharField(max_length=64)
    razorpay_signature = serializers.CharField(max_length=256)


class VerifyView(APIView):
    """Checkout's success callback, checked on the server. The webhook makes
    the same change regardless; this just makes the app current at once."""

    permission_classes = [IsAdmin]

    def post(self, request):
        serializer = VerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        sub = Subscription.objects.filter(
            razorpay_subscription_id=data["razorpay_subscription_id"], restaurant=request.tenant,
        ).first()
        if sub is None:
            return Response({"razorpay_subscription_id": ["Subscription not found."]}, status=404)
        try:
            verify_subscription_payment(data["razorpay_subscription_id"], data["razorpay_payment_id"], data["razorpay_signature"])
        except RazorpayUnavailableError:
            return Response({"razorpay_signature": ["Payment signature is not valid."]}, status=400)
        services.mark_authorised(sub)
        request.tenant.refresh_from_db()
        return Response(current_payload(request.tenant))


class CancelView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        try:
            services.cancel(request.tenant)
        except services.NoSubscription:
            return Response({"detail": "There is no active subscription to cancel."}, status=409)
        except RazorpayUnavailableError as e:
            return Response({"detail": str(e)}, status=502)
        request.tenant.refresh_from_db()
        return Response(current_payload(request.tenant))


class SubscriptionPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPayment
        fields = ["id", "razorpay_payment_id", "razorpay_invoice_id", "amount", "currency", "status", "method",
                  "plan_tier", "period_start", "period_end", "created_at"]


class InvoicesView(APIView):
    """Billing history for the subscription - what DineOS charged this
    restaurant, distinct from the restaurant's own revenue screens."""

    permission_classes = [IsAdmin]

    def get(self, request):
        payments = SubscriptionPayment.objects.filter(restaurant=request.tenant).order_by("-created_at")
        paginator = DineOSPageNumberPagination()
        page = paginator.paginate_queryset(payments, request, view=self)
        return paginator.get_paginated_response(SubscriptionPaymentSerializer(page, many=True).data)
