from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import Notification

User = get_user_model()

_BRANCH_UNSET = object()


def notify(recipient, type, title, body="", data=None, order=None, table=None, branch=_BRANCH_UNSET):
    if branch is _BRANCH_UNSET:
        branch = table.branch if table is not None else recipient.branch
    notification = Notification.objects.create(
        recipient=recipient, branch=branch, type=type, title=title, body=body,
        data=data or {}, order=order, table=table,
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


def notify_role(roles, tenant, type, title, body="", data=None, order=None, table=None, branch=_BRANCH_UNSET):
    """`tenant` is required (not derivable here — unlike every other
    notifications call site, this queries User directly with nothing else
    to scope by). Callers already have the relevant restaurant in hand via
    order.table.restaurant / session.table.restaurant.

    Branch-scoping (2026-09-08, per Shereena's report — a Branch A order
    going READY was also notifying Branch B's server): table/order/branch
    used to only tag the Notification row's own branch field, never
    actually restrict WHO got notified — every matching role got every
    event, restaurant-wide, regardless of branch. Now resolved the same
    way `notify()` resolves it for a single recipient, then used to filter
    the recipient list itself: only staff pinned to that exact branch, plus
    Admin (who has no fixed branch and is meant to see every branch). A
    restaurant with no branch on this event at all (resolved_branch stays
    None — legacy/single-branch setups) skips this filter entirely, same
    restaurant-wide behavior as before.
    """
    if not tenant.notifications_enabled:
        return []
    resolved_branch = branch
    if resolved_branch is _BRANCH_UNSET:
        if table is not None:
            resolved_branch = table.branch
        elif order is not None:
            resolved_branch = order.branch
        else:
            resolved_branch = None
    recipients = User.objects.filter(restaurant=tenant, role__in=roles, is_active=True)
    if resolved_branch is not None:
        recipients = recipients.filter(Q(branch=resolved_branch) | Q(role="ADMIN"))
    return [
        notify(user, type, title, body=body, data=data, order=order, table=table, branch=branch)
        for user in recipients
    ]
