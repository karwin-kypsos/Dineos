from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import KDSDevice


class AnonymousDevice:
    """Stand-in for `request.user` on KDS-authenticated requests (no real User)."""

    is_authenticated = True
    is_anonymous = False


class KDSKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        api_key = request.META.get(settings.KDS_HEADER_NAME)
        if not api_key:
            return None

        try:
            device = KDSDevice.objects.get(api_key=api_key, is_active=True)
        except KDSDevice.DoesNotExist as exc:
            raise AuthenticationFailed("Invalid or inactive KDS device key.") from exc

        device.last_seen_at = timezone.now()
        device.save(update_fields=["last_seen_at"])
        return (AnonymousDevice(), device)
