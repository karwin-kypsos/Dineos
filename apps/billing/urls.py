from django.urls import path

from .cashier_views import (
    CloseShiftView,
    CurrentShiftView,
    DailyCollectionsView,
    OpenShiftView,
    ShiftReconciliationView,
)
from .views import PayBillView, SessionBillView

urlpatterns = [
    path("session/<uuid:session_id>/", SessionBillView.as_view(), name="bills-session"),
    path("payment/", PayBillView.as_view(), name="bills-payment"),
]

cashier_urlpatterns = [
    path("shifts/open/", OpenShiftView.as_view(), name="cashier-shift-open"),
    path("shifts/current/", CurrentShiftView.as_view(), name="cashier-shift-current"),
    path("shifts/<uuid:shift_id>/reconciliation/", ShiftReconciliationView.as_view(), name="cashier-shift-reconciliation"),
    path("shifts/<uuid:shift_id>/close/", CloseShiftView.as_view(), name="cashier-shift-close"),
    path("collections/daily/", DailyCollectionsView.as_view(), name="cashier-collections-daily"),
]
