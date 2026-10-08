"""organization_updated - a live event to every staff app of a restaurant
when its plan, feature flags or status change (2026-10-08, per Karwin), so
the apps can stop polling /v1/auth/me/ for it.

Frame on the staff WebSocket (/ws/staff/), sent to the restaurant's
staff_all group, i.e. every connected staff client:

    {"type": "organization_updated", "data": {...}}

data is the restaurant object exactly as GET /v1/auth/me/ returns it, plus
organization_status (ACTIVE / TRIAL / SUSPENDED / PAYMENT_DUE) - the whole
object, not a diff. It goes out once per change, only when something in it
actually changed, and only after the change is committed.
"""
from django.db import transaction


def organization_payload(restaurant):
    from apps.authentication.serializers import TenantSummarySerializer

    return {**TenantSummarySerializer(restaurant).data, "organization_status": restaurant.status}


def _send(restaurant_id, data):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(f"staff_all_{restaurant_id}", {"type": "organization_updated", "data": data})


def broadcast_if_changed(restaurant, before):
    """`before` is organization_payload() taken before the change."""
    restaurant.refresh_from_db()
    after = organization_payload(restaurant)
    if after != before:
        restaurant_id = restaurant.id
        transaction.on_commit(lambda: _send(restaurant_id, after))
