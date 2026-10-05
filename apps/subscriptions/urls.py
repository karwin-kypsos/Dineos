from django.urls import path

from .views import CancelView, CheckoutView, CurrentSubscriptionView, InvoicesView, VerifyView

urlpatterns = [
    path("current/", CurrentSubscriptionView.as_view(), name="subscriptions-current"),
    path("checkout/", CheckoutView.as_view(), name="subscriptions-checkout"),
    path("verify/", VerifyView.as_view(), name="subscriptions-verify"),
    path("cancel/", CancelView.as_view(), name="subscriptions-cancel"),
    path("invoices/", InvoicesView.as_view(), name="subscriptions-invoices"),
]
