from django.urls import path

from .views import (
    CategoryViewSet,
    CustomerMenuView,
    MenuItemViewSet,
    OrderTakingMenuView,
)

menu_all = MenuItemViewSet.as_view({"get": "list"})
menu_detail = MenuItemViewSet.as_view({"get": "retrieve", "put": "update", "delete": "destroy"})
menu_availability = MenuItemViewSet.as_view({"patch": "toggle_availability"})

category_list = CategoryViewSet.as_view({"get": "list", "post": "create"})
category_detail = CategoryViewSet.as_view({"get": "retrieve", "put": "update", "delete": "destroy"})

urlpatterns = [
    path("customer/<uuid:table_id>/", CustomerMenuView.as_view(), name="menu-customer"),
    path("all/", menu_all, name="menu-all"),
    path("categories/", category_list, name="menu-categories"),
    path("categories/<int:pk>/", category_detail, name="menu-category-detail"),
    path("<int:pk>/availability/", menu_availability, name="menu-item-availability"),
    path("<int:pk>/", menu_detail, name="menu-item-detail"),
    path("", OrderTakingMenuView.as_view(), name="menu-root"),
]
