from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model

from .models import Notification

User = get_user_model()


def notify(recipient, type, title, body="", data=None, order=None, table=None):
    notification = Notification.objects.create(
        recipient=recipient, type=type, title=title, body=body, data=data or {}, order=order, table=table
    )

    if recipient.restaurant.realtime_enabled:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"notifications_{recipient.id}",
                {
                    "type": "notification_new",
                    "notification_id": notification.id,
                    "notification_type": notification.type,
                    "title": notification.title,
                    "body": notification.body,
                },
            )
    return notification


def notify_role(roles, tenant, type, title, body="", data=None, order=None, table=None):
    """`tenant` is required (not derivable here — unlike every other
    notifications call site, this queries User directly with nothing else
    to scope by). Callers already have the relevant restaurant in hand via
    order.table.restaurant / session.table.restaurant.
    """
    if not tenant.notifications_enabled:
        return []
    recipients = User.objects.filter(restaurant=tenant, role__in=roles, is_active=True)
    return [notify(user, type, title, body=body, data=data, order=order, table=table) for user in recipients]
