from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAdminOrManager, IsAnyStaff

from . import services
from .models import Category, MenuItem, PreparedPortion
from .serializers import (
    AddPortionsSerializer,
    CategorySerializer,
    MenuItemCustomerSerializer,
    MenuItemSerializer,
    PreparedPortionSerializer,
)


def _available_today_queryset():
    today_zero_ids = PreparedPortion.objects.filter(
        date=timezone.localdate(), portions_remaining=0
    ).values_list("menu_item_id", flat=True)
    return MenuItem.objects.filter(is_available=True, is_active=True).exclude(id__in=today_zero_ids)


class CustomerMenuView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, table_id):
        items = _available_today_queryset().select_related("category")
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
        items = _available_today_queryset().select_related("category")
        return Response(MenuItemCustomerSerializer(items, many=True).data)

    def post(self, request):
        serializer = MenuItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MenuItemViewSet(viewsets.ModelViewSet):
    """Manager/Admin management view — all items, including unavailable ones."""

    queryset = MenuItem.objects.select_related("category").all()
    serializer_class = MenuItemSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAnyStaff()]
        return [IsAdminOrManager()]

    @action(detail=True, methods=["patch"], url_path="availability")
    def toggle_availability(self, request, pk=None):
        item = self.get_object()
        item.is_available = not item.is_available
        item.save(update_fields=["is_available"])
        return Response(MenuItemSerializer(item).data)


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdminOrManager()]

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
        portions = PreparedPortion.objects.filter(date=timezone.localdate()).select_related("menu_item")
        return Response(PreparedPortionSerializer(portions, many=True).data)


class AddPortionsView(APIView):
    permission_classes = [IsAdminOrManager]

    def patch(self, request, dish_id):
        serializer = AddPortionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        portion = services.add_portions(dish_id, serializer.validated_data["additional_quantity"])

        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                "customers_global",
                {
                    "type": "portions_updated",
                    "menu_item_id": str(portion.menu_item_id),
                    "portions_remaining": portion.portions_remaining,
                },
            )
        return Response(PreparedPortionSerializer(portion).data)
