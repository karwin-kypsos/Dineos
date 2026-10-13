from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from .models import PlatformAdmin, PlatformAdminBlacklistedToken


class PlatformJWTAuthentication(JWTAuthentication):
    """Resolves the token's user_id claim against PlatformAdmin, never
    apps.authentication.User — and rejects any token missing the
    platform_admin claim, so a restaurant-staff JWT can never authenticate
    here even if it somehow validated structurally.
    """

    def get_user(self, validated_token):
        if not validated_token.get("platform_admin"):
            raise InvalidToken("Not a platform admin token.")

        jti = validated_token.get("jti")
        if jti and PlatformAdminBlacklistedToken.objects.filter(jti=jti).exists():
            raise InvalidToken("This session has been logged out.")

        user_id = validated_token[self.get_user_id_claim()]
        try:
            admin = PlatformAdmin.objects.get(id=user_id)
        except PlatformAdmin.DoesNotExist as exc:
            raise InvalidToken("Platform admin not found.") from exc

        if not admin.is_active:
            raise InvalidToken("Platform admin account is inactive.")
        # Sessions opened before a password change or a reissued temporary
        # password end with it (2026-10-13): the token's password version must
        # be the current one. Tokens from before this existed carry none (0),
        # which matches an account whose password never changed since.
        if validated_token.get("pwv", 0) != admin.password_version:
            raise InvalidToken("The password was changed. Sign in again.")
        return admin

    def get_user_id_claim(self):
        from django.conf import settings

        return settings.SIMPLE_JWT["USER_ID_CLAIM"]
