from django.urls import path

from .views import (
    ActiveOrdersView,
    CreateOrderView,
    MyOrdersView,
    OrderCollectedView,
    OrderDetailView,
    OrderItemKitchenStatusView,
    OrderKitchenStatusView,
    OrderServedView,
    OrdersBySessionView,
    OrdersByTableView,
    ReadyOrdersView,
    TakeawayOrderDetailView,
    TakeawayOrderView,
)

urlpatterns = [
    path("", CreateOrderView.as_view(), name="orders-create"),
    path("takeaway/", TakeawayOrderView.as_view(), name="orders-takeaway-create"),
    path("takeaway/<uuid:order_id>/details/", TakeawayOrderDetailView.as_view(), name="orders-takeaway-detail"),
    path("active/", ActiveOrdersView.as_view(), name="orders-active"),
    path("ready/", ReadyOrdersView.as_view(), name="orders-ready"),
    path("mine/", MyOrdersView.as_view(), name="orders-mine"),
    path("session/<uuid:session_id>/", OrdersBySessionView.as_view(), name="orders-by-session"),
    path("table/<uuid:table_id>/", OrdersByTableView.as_view(), name="orders-by-table"),
    path("<uuid:order_id>/status/", OrderKitchenStatusView.as_view(), name="orders-kitchen-status"),
    path(
        "<uuid:order_id>/items/<int:item_id>/status/",
        OrderItemKitchenStatusView.as_view(),
        name="orders-item-kitchen-status",
    ),
    path("<uuid:order_id>/collected/", OrderCollectedView.as_view(), name="orders-collected"),
    path("<uuid:order_id>/served/", OrderServedView.as_view(), name="orders-served"),
    path("<uuid:order_id>/", OrderDetailView.as_view(), name="orders-detail"),
]
