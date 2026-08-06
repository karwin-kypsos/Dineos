from django.conf import settings
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.authentication.models import User
from apps.authentication.serializers import ROLE_METADATA, DineOSTokenObtainPairSerializer
from apps.authentication.services import issue_invite
from apps.restaurant.models import Restaurant
from apps.restaurant.plans import PLAN_PRESETS
from core.permissions import IsPlatformAdmin

from .authentication import PlatformJWTAuthentication
from .models import ImpersonationSession, PlatformActivityLog, PlatformAdmin, PlatformLoginCode
from .serializers import (
    ImpersonationSessionSerializer,
    PlatformActivityLogSerializer,
    PlatformAdminSerializer,
    PlatformLoginSerializer,
    RestaurantSerializer,
    VerifyPlatformLoginCodeSerializer,
    issue_platform_access_token,
)

IMPERSONATION_TTL_MINUTES = 30


class PlatformLoginView(APIView):
    """Step 1 of 2 — password only. No token issued here; a correct
    password sends a 2FA code (returned directly in DEBUG, since there's
    no email/SMS delivery wired up yet — same convention as
    apps.authentication.views.ForgotPasswordView). Step 2 is
    VerifyPlatformLoginView."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PlatformLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        login_code = PlatformLoginCode.issue(serializer.validated_data["admin"])
        response = {"detail": "Enter the 6-digit code to continue.", "requires_2fa": True}
        if settings.DEBUG:
            response["code"] = login_code.code
        return Response(response, status=status.HTTP_200_OK)


class VerifyPlatformLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifyPlatformLoginCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        admin = serializer.validated_data["admin"]
        login_code = serializer.validated_data["login_code"]

        login_code.used_at = timezone.now()
        login_code.save(update_fields=["used_at"])
        admin.last_active_at = timezone.now()
        admin.save(update_fields=["last_active_at"])

        token = issue_platform_access_token(admin)
        return Response({"access": str(token)}, status=status.HTTP_200_OK)


class TenantViewSet(viewsets.ModelViewSet):
    """Super Admin's view of every client restaurant on the platform —
    create new tenants, and flip their per-add-on feature flags live.
    """

    queryset = Restaurant.objects.all()
    serializer_class = RestaurantSerializer
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def create(self, request, *args, **kwargs):
        # Platform-wide defaults (from .env) apply only when the Super Admin
        # doesn't explicitly set a rate for this specific tenant. A plan
        # tier pre-fills max_branches + the add-on flags unless the caller
        # overrides them explicitly.
        from decimal import Decimal

        from django.conf import settings

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        extra = {}
        if "gst_percentage" not in request.data:
            extra["gst_percentage"] = Decimal(str(settings.DEFAULT_GST_PERCENTAGE))
        if "service_charge_percentage" not in request.data:
            extra["service_charge_percentage"] = Decimal(str(settings.DEFAULT_SERVICE_CHARGE_PERCENTAGE))

        plan_tier = serializer.validated_data.get("plan_tier", Restaurant.PlanTier.STARTER)
        preset = PLAN_PRESETS.get(plan_tier, PLAN_PRESETS["STARTER"])
        if "max_branches" not in request.data:
            extra["max_branches"] = preset["max_branches"]
        for flag, value in preset["flags"].items():
            if flag not in request.data:
                extra[flag] = value

        restaurant = serializer.save(**extra)
        PlatformActivityLog.objects.create(
            actor=self.request.user,
            action="TENANT_CREATED",
            restaurant=restaurant,
            description=f"Created tenant '{restaurant.name}' ({restaurant.slug})",
        )

        data = RestaurantSerializer(restaurant).data
        if restaurant.contact_email:
            admin_name = restaurant.contact_name or "Admin"
            admin = User.objects.create_user(
                email=restaurant.contact_email, password=None, name=admin_name,
                role=User.Role.ADMIN, restaurant=restaurant,
            )
            invite = issue_invite(admin)
            data["admin_user_id"] = str(admin.id)
            data["invite_token"] = invite.token
        return Response(data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        restaurant = serializer.save()
        PlatformActivityLog.objects.create(
            actor=self.request.user,
            action="TENANT_UPDATED",
            restaurant=restaurant,
            description=f"Updated tenant '{restaurant.name}' ({restaurant.slug})",
        )


class DashboardView(APIView):
    """Super Admin app's Dashboard screen — platform-wide summary numbers.
    Deliberately cheap aggregate queries only (counts, no joins across
    tenant data) since this runs on every dashboard load.
    """

    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        total_tenants = Restaurant.objects.count()
        active_tenants = Restaurant.objects.filter(is_active=True).count()
        total_staff = User.objects.count()
        recent_tenants = Restaurant.objects.order_by("-created_at")[:5]

        return Response(
            {
                "total_tenants": total_tenants,
                "active_tenants": active_tenants,
                "inactive_tenants": total_tenants - active_tenants,
                "total_staff_across_platform": total_staff,
                "recent_tenants": RestaurantSerializer(recent_tenants, many=True).data,
            }
        )


class ActivityLogListView(ListAPIView):
    """Super Admin app's Activity Log screen — paginated feed of platform
    actions, newest first (see PlatformActivityLog.Meta.ordering).
    """

    serializer_class = PlatformActivityLogSerializer
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    queryset = PlatformActivityLog.objects.select_related("actor", "restaurant").all()


class TeamViewSet(viewsets.ModelViewSet):
    """Super Admin app's Team screen — manage other platform admin accounts.
    No delete endpoint on purpose: matches the soft-deactivate pattern used
    by apps.authentication.StaffViewSet, so historical activity-log entries
    always resolve to a real actor.
    """

    queryset = PlatformAdmin.objects.all().order_by("-created_at")
    serializer_class = PlatformAdminSerializer
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):
        admin = serializer.save()
        PlatformActivityLog.objects.create(
            actor=self.request.user,
            action="TEAM_MEMBER_ADDED",
            description=f"Added team member '{admin.email}'",
        )

    def partial_update(self, request, *args, **kwargs):
        # Only is_active is meant to be toggled from this screen (deactivate
        # a departing team member) — everything else about an admin account
        # is set once at creation.
        instance = self.get_object()
        instance.is_active = request.data.get("is_active", instance.is_active)
        instance.save(update_fields=["is_active"])
        if not instance.is_active:
            PlatformActivityLog.objects.create(
                actor=request.user,
                action="TEAM_MEMBER_DEACTIVATED",
                description=f"Deactivated team member '{instance.email}'",
            )
        return Response(PlatformAdminSerializer(instance).data)


class ImpersonateView(APIView):
    """'Impersonate / Support Access' on Organization Detail. Mints a
    short-lived (30 min) token for the tenant's own first Admin, tagged
    with impersonated_by + impersonation_session_id. Every subsequent
    request on that token is checked against the ImpersonationSession row
    by core.tenancy.TenantResolverMiddleware — ending the session (see
    EndImpersonationView) revokes it immediately, not just at expiry.
    """

    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def post(self, request, restaurant_id):
        from rest_framework.exceptions import NotFound

        restaurant = Restaurant.objects.filter(id=restaurant_id).first()
        if restaurant is None:
            raise NotFound("Organization not found.")

        target_user = User.objects.filter(restaurant=restaurant, role=User.Role.ADMIN, is_active=True).first()
        if target_user is None:
            return Response(
                {"detail": "This organization has no active Admin account to impersonate."},
                status=status.HTTP_409_CONFLICT,
            )

        expires_at = timezone.now() + timezone.timedelta(minutes=IMPERSONATION_TTL_MINUTES)
        session = ImpersonationSession.objects.create(
            platform_admin=request.user, restaurant=restaurant, target_user=target_user, expires_at=expires_at,
        )

        # get_token() returns a RefreshToken (it embeds role/name/restaurant_id
        # claims we want) — derive the actual ACCESS token from it and only
        # ever hand that out; impersonation never issues a refresh token.
        refresh = DineOSTokenObtainPairSerializer.get_token(target_user)
        token = refresh.access_token
        token.set_exp(lifetime=timezone.timedelta(minutes=IMPERSONATION_TTL_MINUTES))
        token["impersonated_by"] = str(request.user.id)
        token["impersonation_session_id"] = str(session.id)

        PlatformActivityLog.objects.create(
            actor=request.user,
            action="TENANT_IMPERSONATED",
            restaurant=restaurant,
            description=f"Started support access as {target_user.email} ({restaurant.name})",
        )

        return Response({
            "access": str(token),
            "role": target_user.role,
            "role_id": ROLE_METADATA[target_user.role]["id"],
            "role_name": ROLE_METADATA[target_user.role]["name"],
            "name": target_user.name,
            "restaurant_id": str(restaurant.id),
            "impersonation_session_id": str(session.id),
            "expires_at": expires_at.isoformat(),
        }, status=status.HTTP_201_CREATED)


class EndImpersonationView(APIView):
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def post(self, request, session_id):
        from rest_framework.exceptions import NotFound

        session = ImpersonationSession.objects.filter(id=session_id, platform_admin=request.user).first()
        if session is None:
            raise NotFound("Impersonation session not found.")

        if session.ended_at is None:
            session.ended_at = timezone.now()
            session.save(update_fields=["ended_at"])
            PlatformActivityLog.objects.create(
                actor=request.user,
                action="IMPERSONATION_ENDED",
                restaurant=session.restaurant,
                description=f"Ended support access as {session.target_user.email} ({session.restaurant.name})",
            )
        return Response(ImpersonationSessionSerializer(session).data)
