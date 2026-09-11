from django.urls import path

from .views import (
    CreateCustomerRazorpayOrderView,
    CreateRazorpayOrderView,
    CreateRazorpayQrCodeView,
    CustomerPaymentStatusView,
    RazorpayWebhookView,
)

urlpatterns = [
    path("razorpay/create-order/", CreateRazorpayOrderView.as_view(), name="payments-razorpay-create-order"),
    path("razorpay/qr-code/", CreateRazorpayQrCodeView.as_view(), name="payments-razorpay-qr-code"),
    path(
        "razorpay/customer/create-order/", CreateCustomerRazorpayOrderView.as_view(),
        name="payments-razorpay-customer-create-order",
    ),
    path(
        "razorpay/customer/status/<uuid:session_id>/", CustomerPaymentStatusView.as_view(),
        name="payments-razorpay-customer-status",
    ),
    path("razorpay/webhook/", RazorpayWebhookView.as_view(), name="payments-razorpay-webhook"),
    # 2026-09-11: Django's APPEND_SLASH turns a POST to the no-slash form into
    # a 301 redirect, and most webhook dispatchers (confirmed live: Razorpay's)
    # follow a 301 by re-sending as GET, silently dropping the POST body. That
    # meant every real Razorpay-delivered webhook was bouncing off this
    # endpoint (POST .../webhook -> 301 -> GET .../webhook/ -> 405), so no
    # payment ever actually got confirmed no matter how many times it was paid
    # for real on Razorpay's side. Accepting both forms directly removes the
    # redirect entirely, regardless of which exact URL ends up saved in
    # Razorpay's dashboard.
    path("razorpay/webhook", RazorpayWebhookView.as_view(), name="payments-razorpay-webhook-noslash"),
]
