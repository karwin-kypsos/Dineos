from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAdminOrManager, IsAnyStaff
from core.tenancy import TenantObjectPermission, get_tenant_from_table

from . import services
from .models import Category, MenuItem, PreparedPortion
from .serializers import (
    AddPortionsSerializer,
    CategorySerializer,
    MenuItemCustomerSerializer,
    MenuItemSerializer,
    PreparedPortionSerializer,
)


def _available_today_queryset(restaurant):
    today_zero_ids = PreparedPortion.objects.filter(
        date=timezone.localdate(), portions_remaining=0
    ).values_list("menu_item_id", flat=True)
    return MenuItem.objects.filter(
        category__restaurant=restaurant, is_available=True, is_active=True
    ).exclude(id__in=today_zero_ids)


class CustomerMenuView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, table_id):
        restaurant = get_tenant_from_table(table_id)
        if restaurant is None:
            return Response({"detail": "Table not found."}, status=status.HTTP_404_NOT_FOUND)
        items = _available_today_queryset(restaurant).select_related("category")
        return Response(MenuItemCustomerSerializer(items, many=True).data)


class OrderTakingMenuView(APIView):
    """GET here is the lean order-taking view (Server/Cashier); POST is
    Manager-only item creation — the doc specifies both on the same path.
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminOrManager()]
        return [IsAnyStaff()]

    def get(self, request):
        items = _available_today_queryset(request.tenant).select_related("category")
        return Response(MenuItemCustomerSerializer(items, many=True).data)

    def post(self, request):
        serializer = MenuItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Category must belong to the caller's own restaurant — never trust
        # a client-supplied category id blindly (cross-tenant injection).
        category = serializer.validated_data["category"]
        if category.restaurant_id != request.tenant.id:
            return Response({"category": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MenuItemViewSet(viewsets.ModelViewSet):
    """Manager/Admin management view — all items, including unavailable ones."""

    serializer_class = MenuItemSerializer

    def get_queryset(self):
        return MenuItem.objects.filter(category__restaurant=self.request.tenant).select_related("category")

    def get_permissions(self):
        base = [IsAnyStaff()] if self.action in ("list", "retrieve") else [IsAdminOrManager()]
        return base + [TenantObjectPermission()]

    def perform_create(self, serializer):
        category = serializer.validated_data["category"]
        if category.restaurant_id != self.request.tenant.id:
            from rest_framework.exceptions import NotFound

            raise NotFound("category not found")
        serializer.save()

    @action(detail=True, methods=["patch"], url_path="availability")
    def toggle_availability(self, request, pk=None):
        item = self.get_object()
        item.is_available = not item.is_available
        item.save(update_fields=["is_available"])
        return Response(MenuItemSerializer(item).data)


class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer

    def get_queryset(self):
        return Category.objects.filter(restaurant=self.request.tenant)

    def get_permissions(self):
        base = [IsAuthenticated()] if self.action in ("list", "retrieve") else [IsAdminOrManager()]
        return base + [TenantObjectPermission()]

    def perform_create(self, serializer):
        serializer.save(restaurant=self.request.tenant)

    def destroy(self, request, *args, **kwargs):
        category = self.get_object()
        if category.items.exists():
            return Response(
                {"detail": "Move all items out of this category before deleting it."},
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


class PreparedDishesTodayView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        portions = PreparedPortion.objects.filter(
            date=timezone.localdate(), menu_item__category__restaurant=request.tenant
        ).select_related("menu_item")
        return Response(PreparedPortionSerializer(portions, many=True).data)


class AddPortionsView(APIView):
    permission_classes = [IsAdminOrManager]

    def patch(self, request, dish_id):
        serializer = AddPortionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # dish_id is a guessable sequential int PK — confirm tenant ownership
        # before mutating anything.
        get_object_or_404(MenuItem, id=dish_id, category__restaurant=request.tenant)

        portion = services.add_portions(dish_id, serializer.validated_data["additional_quantity"])

        if request.tenant.realtime_enabled:
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    f"customers_global_{request.tenant.id}",
                    {
                        "type": "portions_updated",
                        "menu_item_id": str(portion.menu_item_id),
                        "portions_remaining": portion.portions_remaining,
                    },
                )
        return Response(PreparedPortionSerializer(portion).data)
