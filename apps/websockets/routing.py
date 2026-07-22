from django.urls import path

from .consumers import KitchenConsumer, StaffConsumer, TableConsumer
from .middleware import JWTAuthMiddleware, KDSAuthMiddleware, TableSessionAuthMiddleware

websocket_urlpatterns = [
    path("ws/staff/", JWTAuthMiddleware(StaffConsumer.as_asgi())),
    path("ws/kitchen/", KDSAuthMiddleware(KitchenConsumer.as_asgi())),
    path("ws/table/<uuid:session_id>/", TableSessionAuthMiddleware(TableConsumer.as_asgi())),
]
