from django.urls import path

from .views import CreateRazorpayOrderView, RazorpayWebhookView

urlpatterns = [
    path("razorpay/create-order/", CreateRazorpayOrderView.as_view(), name="payments-razorpay-create-order"),
    path("razorpay/webhook/", RazorpayWebhookView.as_view(), name="payments-razorpay-webhook"),
]
