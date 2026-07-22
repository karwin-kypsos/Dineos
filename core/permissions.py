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
