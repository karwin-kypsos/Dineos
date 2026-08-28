from django.urls import path

from .views import (
    MarkAllNotificationsReadView,
    MarkNotificationReadView,
    NotificationListView,
    UnreadCountView,
)

urlpatterns = [
    path("", NotificationListView.as_view(), name="notifications-list"),
    path("unread-count/", UnreadCountView.as_view(), name="notifications-unread-count"),
    path("read-all/", MarkAllNotificationsReadView.as_view(), name="notifications-mark-all-read"),
    path("<int:notification_id>/read/", MarkNotificationReadView.as_view(), name="notifications-mark-read"),
]
