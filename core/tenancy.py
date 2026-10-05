"""Tenant resolution for the multi-tenant platform.

Three realms funnel into one `request.tenant` (a Restaurant instance, or
None if unresolved):
  - Staff JWT: the `restaurant_id` claim embedded at login.
  - Customer (TableSession UUID, no login): resolved explicitly by the
    handful of AllowAny views that need it, via the helpers below.
  - Kitchen (KDS device key): the device's own restaurant FK.

Platform (Super Admin) requests never resolve a tenant — their tokens
carry no restaurant_id by design (see apps/platform/authentication.py).
"""
import uuid

from django.http import JsonResponse
from rest_framework.permissions import BasePermission
from rest_framework_simplejwt.tokens import UntypedToken


class ImpersonationRevoked(Exception):
    """Raised internally when a token carries an impersonation_session_id
    for a session that's been explicitly ended or has expired — see
    apps.platform.views.EndImpersonationView. A stateless JWT can't be
    invalidated by itself, so this DB check is what makes 'end this support
    session now' actually take effect immediately instead of at token
    expiry."""


class TenantPaymentDue(Exception):
    """The restaurant is PAYMENT_DUE (its subscription ended or renewals
    failed - apps.subscriptions). Its Admin may still sign in and renew;
    every other staff request, and its kitchen tablets, get a 403 with
    code "subscription_required" (2026-10-05)."""


# What a PAYMENT_DUE restaurant's Admin can still reach: renewing, and the
# account basics the app needs around it.
PAYMENT_DUE_ALLOWED_PATHS = (
    "/v1/subscriptions/",
    "/v1/auth/me/",
    "/v1/auth/plans/",
    "/v1/auth/logout/",
    "/v1/auth/refresh-token/",
    "/v1/auth/change-password/",
)


class TenantSuspended(Exception):
    """Raised internally when a resolved tenant's status is SUSPENDED —
    see apps.platform.views.TenantViewSet.update_status. Login is already
    blocked at that point (DineOSTokenObtainPairSerializer), but a token
    issued before suspension would otherwise keep working until it
    expires; this makes suspension take effect on every request, not just
    new logins."""


class TenantResolverMiddleware:
    """Best-effort: sets request.tenant from whichever of the staff-JWT or
    KDS-device-key headers is present. Never rejects a request itself —
    actual authentication/authorization still happens in DRF's normal
    authentication/permission classes further down the stack. A request
    resolved by neither (the login-less customer realm) is picked up
    explicitly by the specific views that need it, via the helpers below.

    The one exception: a revoked/expired impersonation token IS rejected
    right here, with a 401 — see ImpersonationRevoked.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            request.tenant = self._resolve(request)
        except ImpersonationRevoked:
            return JsonResponse({"detail": "This support access session has ended."}, status=401)
        except TenantSuspended:
            return JsonResponse({"detail": "This organization's account has been suspended."}, status=403)
        except TenantPaymentDue:
            return JsonResponse(
                {"detail": "This organization's subscription has ended. Renew it to continue.", "code": "subscription_required"},
                status=403,
            )
        return self.get_response(request)

    def _resolve(self, request):
        tenant = self._resolve_from_jwt(request)
        if tenant is not None:
            return tenant
        return self._resolve_from_kds_key(request)

    def _resolve_from_jwt(self, request):
        from django.conf import settings

        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        try:
            token = UntypedToken(auth_header[7:])
        except Exception:  # noqa: BLE001 — any decode/validation failure just means "unresolved"
            return None

        if token.get("platform_admin"):
            return None  # platform tokens never carry a tenant

        impersonation_session_id = token.get("impersonation_session_id")
        if impersonation_session_id:
            from apps.platform.models import ImpersonationSession

            session = ImpersonationSession.objects.filter(id=impersonation_session_id).first()
            if session is None or not session.is_active:
                raise ImpersonationRevoked()

        restaurant_id = token.get("restaurant_id")
        if not restaurant_id:
            return None

        from apps.restaurant.models import Restaurant

        restaurant = Restaurant.objects.filter(id=restaurant_id).first()
        if restaurant is not None and restaurant.status == Restaurant.Status.SUSPENDED:
            raise TenantSuspended()
        if restaurant is not None and restaurant.status == Restaurant.Status.PAYMENT_DUE:
            # Only the Admin, and only what renewing needs.
            if token.get("role") != "ADMIN" or not request.path.startswith(PAYMENT_DUE_ALLOWED_PATHS):
                raise TenantPaymentDue()
        return restaurant

    def _resolve_from_kds_key(self, request):
        api_key = request.META.get("HTTP_X_KDS_API_KEY")
        if not api_key:
            return None

        from apps.kitchen.models import KDSDevice

        device = KDSDevice.objects.filter(api_key=api_key, is_active=True).select_related("restaurant").first()
        if device is None:
            return None
        # 2026-10-05: a suspended or unpaid restaurant's kitchen tablets
        # used to keep working - only staff tokens were checked.
        from apps.restaurant.models import Restaurant

        if device.restaurant.status == Restaurant.Status.SUSPENDED:
            raise TenantSuspended()
        if device.restaurant.status == Restaurant.Status.PAYMENT_DUE:
            raise TenantPaymentDue()
        return device.restaurant


def get_tenant_from_table(table_id):
    from apps.tables.models import Table

    table = Table.objects.filter(id=table_id).select_related("restaurant").first()
    return table.restaurant if table else None


def get_branch_from_table(table_id):
    from apps.tables.models import Table

    table = Table.objects.filter(id=table_id).select_related("branch").first()
    return table.branch if table else None


def get_tenant_from_session(session_id):
    from apps.tables.models import TableSession

    session = TableSession.objects.filter(id=session_id).select_related("table__restaurant").first()
    return session.table.restaurant if session else None


def resolve_report_branch(request):
    """The branch a report or AI endpoint covers; None means the whole
    restaurant.

    2026-09-29, per Karwin. The reporting and AI endpoints (End of Day
    Review, the AI End of Day Report, AI Chat, low-stock alerts, the AI
    Prep Forecast) read every branch at once, so a Manager pinned to one
    branch saw every branch's revenue and stock. The same strict rule
    Tables, Menu and Inventory follow since 2026-09-14/21:

      - staff with a branch always get their own branch. A ?branch= naming
        a different one is refused with 403, the same answer a cashier gets
        for billing another branch, rather than quietly swapped for their
        own - a report that silently ignores the filter reads as if it
        applied;
      - an Admin has no branch of their own, so they get the whole
        restaurant unless they narrow it with ?branch=. A malformed id is
        400 and one that isn't this restaurant's is 404, never an
        unfiltered answer.
    """
    from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

    own_branch = getattr(request.user, "branch", None)
    branch_param = (request.query_params.get("branch") or "").strip()
    if not branch_param:
        return own_branch

    try:
        branch_id = uuid.UUID(branch_param)
    except ValueError:
        raise ValidationError({"branch": ["Expected a valid branch id."]})

    if own_branch is not None:
        if own_branch.id != branch_id:
            raise PermissionDenied("You can only view your own branch.")
        return own_branch

    branch = request.tenant.branches.filter(id=branch_id).first()
    if branch is None:
        raise NotFound({"branch": "Branch not found."})
    return branch


class TenantObjectPermission(BasePermission):
    """Object-level safety net for models keyed by a guessable sequential
    integer PK (Category, MenuItem) — even if a queryset filter is missed
    somewhere, this stops a request from acting on another tenant's row.
    """

    def has_object_permission(self, request, view, obj):
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return False
        obj_restaurant_id = getattr(obj, "restaurant_id", None)
        if obj_restaurant_id is None and hasattr(obj, "category"):
            obj_restaurant_id = obj.category.restaurant_id
        return obj_restaurant_id == tenant.id
