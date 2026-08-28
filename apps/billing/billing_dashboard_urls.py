from django.urls import path

from .billing_dashboard_views import (
    BillingCashierBillsView,
    BillingCashierDetailView,
    BillingCashiersView,
    BillingFloorStatusView,
    BillingPaymentSplitView,
    BillingSummaryView,
)

billing_dashboard_urlpatterns = [
    path("summary/", BillingSummaryView.as_view(), name="billing-summary"),
    path("floor-status/", BillingFloorStatusView.as_view(), name="billing-floor-status"),
    path("payment-split/", BillingPaymentSplitView.as_view(), name="billing-payment-split"),
    path("cashiers/", BillingCashiersView.as_view(), name="billing-cashiers"),
    path("cashiers/<uuid:cashier_id>/", BillingCashierDetailView.as_view(), name="billing-cashier-detail"),
    path("cashiers/<uuid:cashier_id>/bills/", BillingCashierBillsView.as_view(), name="billing-cashier-bills"),
]
