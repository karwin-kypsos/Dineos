from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == "ADMIN")


class IsManager(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == "MANAGER")


class IsServer(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == "SERVER")


class IsCashier(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == "CASHIER")


class IsAdminOrManager(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.role in ("ADMIN", "MANAGER")
        )


class IsCashierOrManager(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.role in ("CASHIER", "MANAGER", "ADMIN")
        )


class IsAnyStaff(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role in ("ADMIN", "MANAGER", "SERVER", "CASHIER")
        )


class IsKDSDevice(BasePermission):
    """Grants access only to requests authenticated via KDSKeyAuthentication."""

    def has_permission(self, request, view):
        from apps.kitchen.models import KDSDevice

        return isinstance(request.auth, KDSDevice)


class IsServerOrKDSDevice(BasePermission):
    def has_permission(self, request, view):
        from apps.kitchen.models import KDSDevice

        if isinstance(request.auth, KDSDevice):
            return True
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role in ("ADMIN", "MANAGER", "SERVER")
        )


class IsPlatformAdmin(BasePermission):
    """Grants access only to requests authenticated via PlatformJWTAuthentication.

    A Support (Read-only) member can look but not change anything: every
    request other than GET/HEAD/OPTIONS is a 403 "Only a Full Admin can do
    this." (2026-10-22, per Karwin - until then only team management was
    refused, and hiding buttons in the app was all that stopped the rest).
    Refused here rather than per endpoint, so a new platform endpoint is
    covered without anyone remembering to. A view whose change is the
    member's own - signing out, their own password, ending their own support
    access - opts out with `read_only_members_may_write = True`.
    """

    def has_permission(self, request, view):
        from apps.platform.models import PlatformAdmin

        admin = request.user
        if not (admin and isinstance(admin, PlatformAdmin) and admin.is_active):
            return False
        if (
            request.method not in SAFE_METHODS
            and admin.access_level != PlatformAdmin.AccessLevel.FULL_ADMIN
            and not getattr(view, "read_only_members_may_write", False)
        ):
            self.message = "Only a Full Admin can do this."
            return False
        return True


# customer_ordering_enabled off (2026-09-30, per Shereena): a customer on the
# table QR can still read the menu, but staff take the orders.
# 2026-10-05: a suspended or unpaid (PAYMENT_DUE) restaurant takes no new
# tables or orders from the QR code.
RESTAURANT_NOT_SERVING = "This restaurant isn't taking orders right now. Please ask a staff member."

CUSTOMER_ORDERING_OFF = (
    "Ordering from the table isn't available at this restaurant. Please ask a staff member to take your order."
)


def FeatureEnabledPermission(flag_name):
    """Factory: a permission class rejecting the request unless the
    resolved tenant (request.tenant, set by core.tenancy.TenantResolverMiddleware)
    has the given add-on flag enabled. Used to gate an entire optional
    module (e.g. billing) at the view layer.
    """

    class _FeatureEnabledPermission(BasePermission):
        message = f"The '{flag_name}' add-on is not enabled for this restaurant."

        def has_permission(self, request, view):
            tenant = getattr(request, "tenant", None)
            return bool(tenant and getattr(tenant, flag_name, False))

    _FeatureEnabledPermission.__name__ = f"FeatureEnabledPermission_{flag_name}"
    return _FeatureEnabledPermission
