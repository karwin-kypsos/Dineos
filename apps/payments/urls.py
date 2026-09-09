from django.urls import path

from .views import (
    CreateCustomerRazorpayOrderView,
    CreateRazorpayOrderView,
    CreateRazorpayQrCodeView,
    RazorpayWebhookView,
)

urlpatterns = [
    path("razorpay/create-order/", CreateRazorpayOrderView.as_view(), name="payments-razorpay-create-order"),
    path("razorpay/qr-code/", CreateRazorpayQrCodeView.as_view(), name="payments-razorpay-qr-code"),
    path(
        "razorpay/customer/create-order/", CreateCustomerRazorpayOrderView.as_view(),
        name="payments-razorpay-customer-create-order",
    ),
    path("razorpay/webhook/", RazorpayWebhookView.as_view(), name="payments-razorpay-webhook"),
]
