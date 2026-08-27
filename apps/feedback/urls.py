from django.urls import path

from .views import FeedbackListView, SubmitFeedbackView

urlpatterns = [
    path("", FeedbackListView.as_view(), name="feedback-list"),
    path("submit/", SubmitFeedbackView.as_view(), name="feedback-submit"),
]
