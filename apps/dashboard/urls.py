from django.urls import path

from .views import AdminDashboardView, EndOfDayReviewView, LowStockAlertsView

urlpatterns = [
    path("dashboard/", AdminDashboardView.as_view(), name="admin-dashboard"),
    path("eod-review/", EndOfDayReviewView.as_view(), name="admin-eod-review"),
    path("ai/low-stock-alerts/", LowStockAlertsView.as_view(), name="admin-ai-low-stock-alerts"),
]
