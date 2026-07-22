from django.urls import path

from .views import AddPortionsView, PreparedDishesTodayView

urlpatterns = [
    path("today/", PreparedDishesTodayView.as_view(), name="prepared-dishes-today"),
    path("<int:dish_id>/add-portions/", AddPortionsView.as_view(), name="prepared-dishes-add-portions"),
]
