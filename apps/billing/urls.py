from django.urls import path

from .views import PayBillView, SessionBillView

urlpatterns = [
    path("session/<uuid:session_id>/", SessionBillView.as_view(), name="bills-session"),
    path("payment/", PayBillView.as_view(), name="bills-payment"),
]
