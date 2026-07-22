"""Per-route WebSocket auth middlewares — each wraps only its own consumer
in routing.py, so there is no path-sniffing branch logic in any one of them.
"""

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser


class JWTAuthMiddleware:
    """Authenticate staff WebSocket connections via JWT — mirrors the
    sibling Super-Agent-Platform project's apps/websockets/middleware.py
    exactly (query string `?token=` first, else `Authorization: Bearer`).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode()
        params = parse_qs(query_string)
        token = None

        if "token" in params:
            token = params["token"][0]
        else:
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]

        if token:
            scope["user"] = await _get_user_from_token(token)
        else:
            scope["user"] = AnonymousUser()

        return await self.app(scope, receive, send)


@database_sync_to_async
def _get_user_from_token(token):
    from django.contrib.auth import get_user_model
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.tokens import AccessToken

    User = get_user_model()
    try:
        validated = AccessToken(token)
        return User.objects.get(id=validated["user_id"])
    except (TokenError, User.DoesNotExist):
        return AnonymousUser()


class KDSAuthMiddleware:
    """Resolves the `?kds_key=` query param to a KDSDevice (or None) and
    sets scope["kds_device"] — does NOT reject here; validity is judged by
    KitchenConsumer.connect() so both "missing key" and "invalid key" get
    the same clean 4003 rather than a generic connection failure.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode()
        params = parse_qs(query_string)
        kds_key = params.get("kds_key", [None])[0]
        scope["kds_device"] = await _get_device_from_key(kds_key) if kds_key else None
        return await self.app(scope, receive, send)


@database_sync_to_async
def _get_device_from_key(kds_key):
    from apps.kitchen.models import KDSDevice

    return KDSDevice.objects.filter(api_key=kds_key, is_active=True).first()


class TableSessionAuthMiddleware:
    """Deliberately cheap — no DB hit here. `session_id` comes straight from
    the URL route kwargs; TableConsumer.connect() does the actual existence
    and open/closed check (the fix for the sibling's known ownership-check
    gap — see plan doc).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        return await self.app(scope, receive, send)
