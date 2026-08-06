from django.conf import settings
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
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
from .constants import FEATURE_FLAG_METADATA, THEME_COLOR_PRESETS
from .models import (
    ImpersonationSession,
    PlatformActivityLog,
    PlatformAdmin,
    PlatformAdminBlacklistedToken,
    PlatformLoginCode,
    PlatformRefreshToken,
)
from .serializers import (
    ImpersonationSessionSerializer,
    PlatformActivityLogSerializer,
    PlatformAdminSerializer,
    PlatformLoginSerializer,
    PlatformRefreshSerializer,
    RestaurantSerializer,
    VerifyPlatformLoginCodeSerializer,
    issue_platform_access_token,
)

IMPERSONATION_TTL_MINUTES = 30
FEATURE_FLAG_KEYS = {f["key"] for f in FEATURE_FLAG_METADATA}


class PlatformLoginView(APIView):
    """Step 1 of 2 — password only. No token issued here; a correct
    password sends a 2FA code (step 2 is VerifyPlatformLoginView). Until
    settings.EMAIL_DELIVERY_ENABLED is turned on, there's no real email/SMS
    channel to deliver that code through, so PlatformLoginCode issues a
    fixed, well-known code instead of a random one (see
    apps.platform.models._generate_2fa_code) — the response echoes it back
    either way so the flow is testable end-to-end without one."""

    # No authentication_classes: without this, DRF still runs the global
    # default (JWTAuthentication against apps.authentication.User) against
    # any stray Authorization header the caller happens to send — a token
    # that doesn't resolve there raises AuthenticationFailed and 401s the
    # request outright, before AllowAny is even consulted.
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PlatformLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        login_code = PlatformLoginCode.issue(serializer.validated_data["admin"])
        response = {"detail": "Enter the 6-digit code to continue.", "requires_2fa": True}
        if settings.DEBUG or not settings.EMAIL_DELIVERY_ENABLED:
            response["code"] = login_code.code
        return Response(response, status=status.HTTP_200_OK)


class VerifyPlatformLoginView(APIView):
    authentication_classes = []
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
        refresh = PlatformRefreshToken.issue(admin)
        return Response({"access": str(token), "refresh": refresh.token}, status=status.HTTP_200_OK)


class PlatformRefreshView(APIView):
    """Exchanges a still-valid refresh token for a new access token,
    without redoing password + 2FA. Deliberately doesn't rotate the
    refresh token on use (unlike rest_framework_simplejwt's optional
    rotation) — simpler, and adequate for the small, trusted set of
    Krypsos team accounts this serves; the refresh token itself still
    expires (7 days) and can be revoked at logout."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PlatformRefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        refresh_token = serializer.validated_data["refresh_token"]
        token = issue_platform_access_token(refresh_token.admin)
        return Response({"access": str(token)}, status=status.HTTP_200_OK)


class PlatformLogoutView(APIView):
    """Blacklists the current access token's jti immediately (see
    PlatformAdminBlacklistedToken / PlatformJWTAuthentication), and — if
    a refresh token is passed in the body, matching apps.authentication.
    views.LogoutView's pattern — revokes that too, so a stolen refresh
    token can't be used to mint fresh access tokens after logout."""

    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def post(self, request):
        jti = request.auth.get("jti") if request.auth else None
        if jti:
            PlatformAdminBlacklistedToken.objects.get_or_create(jti=jti)

        refresh = request.data.get("refresh")
        if refresh:
            PlatformRefreshToken.objects.filter(token=refresh, admin=request.user).update(revoked_at=timezone.now())

        return Response(status=status.HTTP_204_NO_CONTENT)


class SubscriptionPlansView(APIView):
    """Create Organization screen's Plan Tier dropdown — each preset's
    max_branches + starting flags, so the frontend can auto-fill the rest
    of the form the moment a tier is picked (still editable before save).
    """

    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response([
            {"tier": tier, "label": tier.title(), "max_branches": preset["max_branches"], "flags": preset["flags"]}
            for tier, preset in PLAN_PRESETS.items()
        ])


class FeatureFlagListView(APIView):
    """Organization Detail's Feature Flags section — the label/description
    text next to each toggle. Static, platform-wide metadata; the actual
    on/off state per organization lives on the Restaurant row itself."""

    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response(FEATURE_FLAG_METADATA)


class ThemeColorListView(APIView):
    """Branding section's primary-color picker — a curated quick-pick
    list; primary_color itself still accepts any hex value."""

    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response(THEME_COLOR_PRESETS)


class TenantViewSet(viewsets.ModelViewSet):
    """Super Admin's view of every client restaurant on the platform —
    create new tenants, and flip their per-add-on feature flags live.
    """

    queryset = Restaurant.objects.all()
    serializer_class = RestaurantSerializer
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get_queryset(self):
        qs = Restaurant.objects.all().order_by("-created_at")

        status_filter = self.request.query_params.get("status", "all")
        if status_filter != "all" and status_filter in Restaurant.Status.values:
            qs = qs.filter(status=status_filter)

        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(name__icontains=search)

        return qs

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
        plan_tier_changed = "plan_tier" in self.request.data and serializer.instance.plan_tier != self.request.data.get(
            "plan_tier"
        )
        restaurant = serializer.save()
        PlatformActivityLog.objects.create(
            actor=self.request.user,
            action="PLAN_CHANGED" if plan_tier_changed else "TENANT_UPDATED",
            restaurant=restaurant,
            description=(
                f"Changed '{restaurant.name}' to the {restaurant.get_plan_tier_display()} plan"
                if plan_tier_changed
                else f"Updated tenant '{restaurant.name}' ({restaurant.slug})"
            ),
        )

    @action(detail=True, methods=["patch"], url_path="status")
    def update_status(self, request, pk=None):
        """Organization Detail's Active/Suspended toggle. Suspending
        blocks staff logins and every staff-authenticated API call for
        every branch in this org immediately (see DineOSTokenObtainPairSerializer
        and core.tenancy.TenantResolverMiddleware) — not just a cosmetic
        status label."""

        restaurant = self.get_object()
        new_status = request.data.get("status")
        if new_status not in Restaurant.Status.values:
            return Response(
                {"status": f"Must be one of {list(Restaurant.Status.values)}."}, status=status.HTTP_400_BAD_REQUEST
            )

        restaurant.status = new_status
        restaurant.is_active = new_status != Restaurant.Status.SUSPENDED
        restaurant.save(update_fields=["status", "is_active"])

        PlatformActivityLog.objects.create(
            actor=request.user,
            action="STATUS_CHANGED",
            restaurant=restaurant,
            description=f"Set '{restaurant.name}' status to {restaurant.get_status_display()}",
        )
        return Response(RestaurantSerializer(restaurant).data)

    @action(detail=True, methods=["patch"], url_path="feature-flags")
    def update_feature_flags(self, request, pk=None):
        """Organization Detail's Feature Flags toggles — instant-save,
        one dedicated endpoint so the frontend doesn't need to resend the
        entire org record for a single switch flip."""

        restaurant = self.get_object()
        unknown = set(request.data.keys()) - FEATURE_FLAG_KEYS
        if unknown:
            return Response({"detail": f"Unknown flag(s): {sorted(unknown)}"}, status=status.HTTP_400_BAD_REQUEST)

        changed = []
        for key, value in request.data.items():
            setattr(restaurant, key, bool(value))
            changed.append(key)
        if changed:
            restaurant.save(update_fields=changed)
            PlatformActivityLog.objects.create(
                actor=request.user,
                action="FLAGS_CHANGED",
                restaurant=restaurant,
                description=f"Changed flags for '{restaurant.name}': {', '.join(changed)}",
            )
        return Response(RestaurantSerializer(restaurant).data)


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

    def get_queryset(self):
        qs = PlatformActivityLog.objects.select_related("actor", "restaurant").all()

        action_filter = self.request.query_params.get("action")
        if action_filter:
            qs = qs.filter(action=action_filter)

        date_from = self.request.query_params.get("date_from")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)

        date_to = self.request.query_params.get("date_to")
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        search = self.request.query_params.get("search", "").strip()
        if search:
            from django.db.models import Q

            qs = qs.filter(Q(restaurant__name__icontains=search) | Q(description__icontains=search))

        return qs


class TeamViewSet(viewsets.ModelViewSet):
    """Super Admin app's Team screen — manage other platform admin accounts.
    Deactivating (partial_update with only is_active) keeps historical
    activity-log entries resolving to a real actor; a real delete is also
    available (destroy) for "Remove" on the Team screen — PlatformActivityLog.
    actor is on_delete=SET_NULL, so past log entries survive the removal.
    """

    queryset = PlatformAdmin.objects.all().order_by("-created_at")
    serializer_class = PlatformAdminSerializer
    authentication_classes = [PlatformJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def perform_create(self, serializer):
        admin = serializer.save()
        PlatformActivityLog.objects.create(
            actor=self.request.user,
            action="TEAM_MEMBER_ADDED",
            description=f"Added team member '{admin.email}'",
        )

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        was_active = instance.is_active

        for field in ("name", "access_level"):
            if field in request.data:
                setattr(instance, field, request.data[field])
        if "is_active" in request.data:
            instance.is_active = request.data["is_active"]
        instance.save()

        if was_active and not instance.is_active:
            PlatformActivityLog.objects.create(
                actor=request.user,
                action="TEAM_MEMBER_DEACTIVATED",
                description=f"Deactivated team member '{instance.email}'",
            )
        return Response(PlatformAdminSerializer(instance).data)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        email = instance.email
        PlatformActivityLog.objects.create(
            actor=request.user,
            action="TEAM_MEMBER_REMOVED",
            description=f"Removed team member '{email}'",
        )
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
