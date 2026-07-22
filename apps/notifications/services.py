from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model

from .models import Notification

User = get_user_model()


def notify(recipient, type, title, body="", data=None, order=None, table=None):
    notification = Notification.objects.create(
        recipient=recipient, type=type, title=title, body=body, data=data or {}, order=order, table=table
    )

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


def notify_role(roles, type, title, body="", data=None, order=None, table=None):
    recipients = User.objects.filter(role__in=roles, is_active=True)
    return [notify(user, type, title, body=body, data=data, order=order, table=table) for user in recipients]
