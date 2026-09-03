from datetime import datetime

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import NotificationSerializer


class NotificationListView(APIView):
    """Per Shereena's spec (2026-08-28): the notification screen shows
    today's notifications by default — older ones stay in the database
    (see the cleanup_notifications management command for the eventual
    7-30 day purge) rather than being deleted the moment they scroll out
    of view, so ?all=true or an explicit ?date= can still reach them.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = Notification.objects.filter(recipient=request.user)

        if request.query_params.get("all") != "true":
            date_param = request.query_params.get("date")
            if date_param:
                try:
                    target_date = datetime.strptime(date_param, "%Y-%m-%d").date()
                except ValueError:
                    target_date = timezone.localdate()
            else:
                target_date = timezone.localdate()
            notifications = notifications.filter(created_at__date=target_date)

        return Response(NotificationSerializer(notifications, many=True).data)


class UnreadCountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return Response({"count": count})


class MarkNotificationReadView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, notification_id):
        notification = Notification.objects.get(id=notification_id, recipient=request.user)
        notification.is_read = True
        notification.save(update_fields=["is_read"])
        return Response(NotificationSerializer(notification).data)


class MarkAllNotificationsReadView(APIView):
    """'Mark all as read' — Karwin (2026-08-28). Only ever touches the
    calling user's own still-unread notifications, so it's safe to call
    repeatedly (a second call is just a no-op update)."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        updated = Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response({"marked_read": updated})


class ClearAllNotificationsView(APIView):
    """'Clear all' — Karwin (2026-09-03). Only ever deletes the calling
    user's own notifications (read or unread), same recipient-scoping as
    every other view here — one staff member's clear-all can never touch
    another's rows."""

    permission_classes = [IsAuthenticated]

    def delete(self, request):
        deleted, _ = Notification.objects.filter(recipient=request.user).delete()
        return Response({"deleted": deleted})
