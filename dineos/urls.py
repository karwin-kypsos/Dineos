from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.billing.urls import cashier_urlpatterns
from apps.restaurant.views import RestaurantSettingsView


def healthz(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="health"),
    path("v1/auth/", include("apps.authentication.urls")),
    path("v1/staff/", include("apps.authentication.staff_urls")),
    path("v1/branches/", include("apps.restaurant.urls")),
    path("v1/admin/settings/", RestaurantSettingsView.as_view(), name="admin-settings"),
    path("v1/tables/", include("apps.tables.urls")),
    path("v1/menu/", include("apps.menu.urls")),
    path("v1/prepared-dishes/", include("apps.menu.prepared_urls")),
    path("v1/inventory/", include("apps.inventory.urls")),
    path("v1/kitchen/", include("apps.kitchen.urls")),
    path("v1/admin/", include("apps.dashboard.urls")),
    path("v1/orders/", include("apps.orders.urls")),
    path("v1/bills/", include("apps.billing.urls")),
    path("v1/cashier/", include(cashier_urlpatterns)),
    path("v1/notifications/", include("apps.notifications.urls")),
    path("platform/", include("apps.platform.urls")),
]
