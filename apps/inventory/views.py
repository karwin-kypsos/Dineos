from django.db import models as dj_models
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.permissions import IsAdminOrManager, IsAnyStaff

from . import services
from .models import Ingredient, PurchaseOrder, RecipeItem
from .serializers import (
    AddStockSerializer,
    IngredientSerializer,
    PurchaseOrderCreateSerializer,
    PurchaseOrderSerializer,
    RecipeItemSerializer,
    RecordWastageSerializer,
)


def _branch_scoped(qs, request):
    branch = getattr(request.user, "branch", None)
    if branch is not None:
        qs = qs.filter(dj_models.Q(branch=branch) | dj_models.Q(branch__isnull=True))
    return qs


class IngredientViewSet(viewsets.ModelViewSet):
    serializer_class = IngredientSerializer

    def get_queryset(self):
        qs = Ingredient.objects.filter(restaurant=self.request.tenant, is_active=True)
        qs = _branch_scoped(qs, self.request)
        if self.request.query_params.get("low_stock") == "true":
            qs = qs.filter(current_stock__lte=dj_models.F("minimum_stock_level"))
        stock_status = self.request.query_params.get("stock_status")
        if stock_status == "critical":
            qs = qs.filter(current_stock__lte=0)
        elif stock_status == "low":
            qs = qs.filter(current_stock__gt=0, current_stock__lte=dj_models.F("minimum_stock_level"))
        elif stock_status == "healthy":
            qs = qs.filter(current_stock__gt=dj_models.F("minimum_stock_level"))
        return qs

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAnyStaff()]
        return [IsAdminOrManager()]

    def perform_create(self, serializer):
        serializer.save(restaurant=self.request.tenant, branch=getattr(self.request.user, "branch", None))

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active"])

    @action(detail=True, methods=["patch"], url_path="add-stock")
    def add_stock(self, request, pk=None):
        ingredient = self.get_object()
        serializer = AddStockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.add_stock(
            ingredient.id, serializer.validated_data["quantity"],
            unit_cost=serializer.validated_data.get("unit_cost"), recorded_by=request.user,
        )
        ingredient.refresh_from_db()
        return Response(IngredientSerializer(ingredient).data)

    @action(detail=True, methods=["patch"], url_path="record-wastage")
    def record_wastage(self, request, pk=None):
        ingredient = self.get_object()
        serializer = RecordWastageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.record_wastage(
                ingredient.id, serializer.validated_data["quantity"],
                serializer.validated_data["wastage_reason"], reason=serializer.validated_data.get("reason", ""),
                recorded_by=request.user,
            )
        except services.InsufficientStockError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        ingredient.refresh_from_db()
        return Response(IngredientSerializer(ingredient).data)


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAdminOrManager]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = PurchaseOrder.objects.filter(restaurant=self.request.tenant)
        return _branch_scoped(qs, self.request)

    def create(self, request, *args, **kwargs):
        serializer = PurchaseOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        for line in serializer.validated_data["lines"]:
            if line["ingredient"].restaurant_id != request.tenant.id:
                return Response({"lines": "Ingredient not found."}, status=status.HTTP_404_NOT_FOUND)
        po = services.create_purchase_order(
            restaurant=request.tenant, branch=getattr(request.user, "branch", None),
            lines=serializer.validated_data["lines"],
            supplier_name=serializer.validated_data["supplier_name"],
            supplier_notes=serializer.validated_data["supplier_notes"],
            requested_by=request.user,
        )
        return Response(PurchaseOrderSerializer(po).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        try:
            po = services.approve_purchase_order(pk, approved_by=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
        return Response(PurchaseOrderSerializer(po).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        try:
            po = services.reject_purchase_order(pk, rejected_by=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
        return Response(PurchaseOrderSerializer(po).data)

    @action(detail=True, methods=["post"], url_path="mark-ordered")
    def mark_ordered(self, request, pk=None):
        try:
            po = services.mark_purchase_order_ordered(pk)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
        return Response(PurchaseOrderSerializer(po).data)

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        try:
            po = services.receive_purchase_order(pk, recorded_by=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
        return Response(PurchaseOrderSerializer(po).data)


class RecipeItemViewSet(viewsets.ModelViewSet):
    serializer_class = RecipeItemSerializer
    permission_classes = [IsAdminOrManager]

    def get_queryset(self):
        qs = RecipeItem.objects.filter(ingredient__restaurant=self.request.tenant)
        menu_item_id = self.request.query_params.get("menu_item")
        if menu_item_id:
            qs = qs.filter(menu_item_id=menu_item_id)
        ingredient_id = self.request.query_params.get("ingredient")
        if ingredient_id:
            qs = qs.filter(ingredient_id=ingredient_id)
        return qs
