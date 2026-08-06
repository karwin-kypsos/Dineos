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

        return Restaurant.objects.filter(id=restaurant_id).first()

    def _resolve_from_kds_key(self, request):
        api_key = request.META.get("HTTP_X_KDS_API_KEY")
        if not api_key:
            return None

        from apps.kitchen.models import KDSDevice

        device = KDSDevice.objects.filter(api_key=api_key, is_active=True).select_related("restaurant").first()
        return device.restaurant if device else None


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
