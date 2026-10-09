"""Admin Self-Registration (2026-09-29, requested by Shereena via Karwin).

A restaurant signs itself up instead of a Super Admin creating it:

  1. GET  /v1/auth/plans/               - the plans to choose from
  2. POST /v1/auth/register-restaurant/ - details + plan -> registration_id
  3. POST /v1/auth/signup/              - registration_id + the Admin's login
  4. POST /v1/auth/login/               - unchanged; a self-registered Admin
                                          logs in like any other

All three are public (no token), so none of them authenticates - a stale
token left in the client must not turn a signup into a 401 - and both
POSTs are rate-limited per client against scripted sign-ups.
"""
from decimal import Decimal

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.platform.constants import FEATURE_FLAG_METADATA
from apps.restaurant.models import Restaurant
from apps.restaurant.plans import PLAN_PRESETS

from . import services
from .serializers import ROLE_METADATA, RegisterRestaurantSerializer, SelfSignupSerializer


def plan_payload(tier):
    from apps.subscriptions.services import plan_price

    preset, price = PLAN_PRESETS[tier], plan_price(tier)
    return {
        "id": tier,
        "name": Restaurant.PlanTier(tier).label,
        # null until PLAN_PRICE_<TIER> is set (settings.PLAN_PRICES)
        "price": str(price.quantize(Decimal("0.01"))) if price is not None else None,
        "currency": "INR",
        "billing_cycle": "MONTHLY" if price is not None else None,
        # Length of the free trial a new restaurant gets (2026-10-09), so the
        # signup screen can say "14-day free trial" without hardcoding it.
        "trial_days": settings.TRIAL_DAYS,
        "max_branches": preset["max_branches"],  # null = unlimited
        "features": [
            {**flag, "included": preset["flags"][flag["key"]]} for flag in FEATURE_FLAG_METADATA
        ],
    }


class PlansView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response([plan_payload(tier) for tier in PLAN_PRESETS])


class RegisterRestaurantView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "self_registration"

    def post(self, request):
        serializer = RegisterRestaurantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        registration = services.start_self_registration(serializer.validated_data)
        return Response(
            {
                "registration_id": str(registration.id),
                "status": registration.status,
                "expires_at": registration.expires_at,
                "restaurant_name": registration.restaurant_name,
                "plan": plan_payload(registration.plan_tier),
                "next_step": "signup",
            },
            status=status.HTTP_201_CREATED,
        )


class SignupView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "self_registration"

    def post(self, request):
        serializer = SelfSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            restaurant, admin = services.complete_self_registration(
                data["registration_id"], data["email"], data["password"],
            )
        except services.RegistrationNotFound:
            return Response({"registration_id": ["Registration not found."]}, status=status.HTTP_404_NOT_FOUND)
        except services.RegistrationAlreadyCompleted:
            return Response(
                {"registration_id": ["This registration is already complete. Log in with the email and password you set."]},
                status=status.HTTP_409_CONFLICT,
            )
        except services.RegistrationExpired:
            return Response(
                {"registration_id": ["This registration has expired. Please register the restaurant again."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except services.EmailAlreadyRegistered:
            return Response(
                {"email": ["An account with this email already exists."]}, status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "detail": "Account created. Log in with this email and password.",
                "registration_id": str(data["registration_id"]),
                "status": "COMPLETED",
                "restaurant_id": str(restaurant.id),
                "restaurant_name": restaurant.name,
                "restaurant_status": restaurant.status,
                "trial_ends_at": restaurant.trial_ends_at,
                "plan_id": restaurant.plan_tier,
                "user_id": str(admin.id),
                "email": admin.email,
                "role": admin.role,
                "role_id": ROLE_METADATA[admin.role]["id"],
                "role_name": ROLE_METADATA[admin.role]["name"],
            },
            status=status.HTTP_201_CREATED,
        )
