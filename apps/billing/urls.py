from django.urls import path

from .cashier_views import (
    CashierCollectionsView,
    CloseShiftView,
    CurrentShiftView,
    DailyCollectionsView,
    MySalesView,
    OpenShiftView,
    ShiftReconciliationView,
)
from .views import BillDetailView, BillListView, PayBillView, PayTakeawayBillView, SessionBillView, TakeawayBillView

urlpatterns = [
    path("", BillListView.as_view(), name="bills-list"),
    path("session/<uuid:session_id>/", SessionBillView.as_view(), name="bills-session"),
    path("payment/", PayBillView.as_view(), name="bills-payment"),
    path("takeaway/<uuid:order_id>/", TakeawayBillView.as_view(), name="bills-takeaway"),
    path("takeaway-payment/", PayTakeawayBillView.as_view(), name="bills-takeaway-payment"),
    path("<uuid:bill_id>/", BillDetailView.as_view(), name="bills-detail"),
]

cashier_urlpatterns = [
    path("shifts/open/", OpenShiftView.as_view(), name="cashier-shift-open"),
    path("shifts/current/", CurrentShiftView.as_view(), name="cashier-shift-current"),
    path("shifts/<uuid:shift_id>/reconciliation/", ShiftReconciliationView.as_view(), name="cashier-shift-reconciliation"),
    path("shifts/<uuid:shift_id>/close/", CloseShiftView.as_view(), name="cashier-shift-close"),
    path("collections/daily/", DailyCollectionsView.as_view(), name="cashier-collections-daily"),
    path("collections/my-sales/", MySalesView.as_view(), name="cashier-collections-my-sales"),
    path("collections/by-cashier/", CashierCollectionsView.as_view(), name="cashier-collections-by-cashier"),
]
