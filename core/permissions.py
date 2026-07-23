from rest_framework.permissions import BasePermission


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
    """Grants access only to requests authenticated via PlatformJWTAuthentication."""

    def has_permission(self, request, view):
        from apps.platform.models import PlatformAdmin

        return bool(request.user and isinstance(request.user, PlatformAdmin) and request.user.is_active)


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
