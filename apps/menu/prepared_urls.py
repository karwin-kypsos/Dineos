from django.urls import path

from .views import AddPortionsView, PrepForecastView, PreparedDishesTodayView

urlpatterns = [
    path("today/", PreparedDishesTodayView.as_view(), name="prepared-dishes-today"),
    path("<int:dish_id>/add-portions/", AddPortionsView.as_view(), name="prepared-dishes-add-portions"),
    path("prep-forecast/", PrepForecastView.as_view(), name="prepared-dishes-prep-forecast"),
]
