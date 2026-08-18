import uuid
from decimal import Decimal

from django.db import models as dj_models
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from core.ai_client import AIUnavailableError
from core.permissions import IsAdminOrManager, IsAnyStaff

from . import services
from .models import AIInsight, Ingredient, PurchaseOrder, RecipeItem, StockMovement
from .serializers import (
    AddStockSerializer,
    AIInsightSerializer,
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


class WastageLogView(APIView):
    """Record Wastage screen's 'Today's Wastage So Far' + 'Today's Wastage
    Log' — total cost, a breakdown by reason, and the individual entries
    for one day. Defaults to today; ?date=YYYY-MM-DD for a past day.
    """

    permission_classes = [IsAdminOrManager]

    def get(self, request):
        from datetime import datetime

        date_param = request.query_params.get("date")
        if date_param:
            try:
                target_date = datetime.strptime(date_param, "%Y-%m-%d").date()
            except ValueError:
                return Response({"date": "Expected format YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            target_date = timezone.localdate()

        movements = StockMovement.objects.filter(
            ingredient__restaurant=request.tenant,
            movement_type=StockMovement.MovementType.WASTAGE,
            recorded_at__date=target_date,
        ).select_related("ingredient", "recorded_by")

        branch = getattr(request.user, "branch", None)
        if branch is not None:
            movements = movements.filter(
                dj_models.Q(ingredient__branch=branch) | dj_models.Q(ingredient__branch__isnull=True)
            )

        breakdown_by_reason = {reason: Decimal("0") for reason in StockMovement.WastageReason.values}
        total_cost = Decimal("0")
        entries = []
        for m in movements.order_by("-recorded_at"):
            cost = m.quantity * (m.unit_cost_at_time or Decimal("0"))
            total_cost += cost
            breakdown_by_reason[m.wastage_reason] += cost
            entries.append({
                "id": str(m.id),
                "ingredient_id": str(m.ingredient_id),
                "ingredient_name": m.ingredient.name,
                "unit": m.ingredient.unit,
                "quantity": m.quantity,
                "wastage_reason": m.wastage_reason,
                "reason": m.reason,
                "cost": cost,
                "recorded_at": m.recorded_at,
                "recorded_by_name": m.recorded_by.name if m.recorded_by else None,
            })

        return Response({
            "date": target_date.isoformat(),
            "total_cost": total_cost,
            "breakdown_by_reason": breakdown_by_reason,
            "entries": entries,
        })


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAdminOrManager]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = PurchaseOrder.objects.filter(restaurant=self.request.tenant)
        qs = _branch_scoped(qs, self.request)

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            try:
                uuid.UUID(branch_id)
                qs = qs.filter(branch_id=branch_id)
            except ValueError:
                pass  # malformed branch id — no filter applied, same convention as StaffViewSet

        needs_action = self.request.query_params.get("needs_action", "").strip().lower()
        if needs_action == "true":
            qs = qs.filter(status=PurchaseOrder.Status.PENDING)

        is_emergency = self.request.query_params.get("is_emergency", "").strip().lower()
        if is_emergency == "true":
            qs = qs.filter(is_emergency=True)
        elif is_emergency == "false":
            qs = qs.filter(is_emergency=False)

        status_filter = self.request.query_params.get("status", "").strip().upper()
        if status_filter in PurchaseOrder.Status.values:
            qs = qs.filter(status=status_filter)

        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                dj_models.Q(supplier_name__icontains=search) | dj_models.Q(lines__ingredient__name__icontains=search)
            ).distinct()

        return qs

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
            reason=serializer.validated_data["reason"],
            is_emergency=serializer.validated_data["is_emergency"],
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


class AIInsightViewSet(viewsets.ReadOnlyModelViewSet):
    """Manager Home / Stock screens' 'AI Insights' feed — read-only list +
    two actions: generate (Groq call, creates fresh rows) and dismiss
    (per-insight, matching the swipeable/dismissable alert cards in the app).
    """

    serializer_class = AIInsightSerializer
    permission_classes = [IsAdminOrManager]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = AIInsight.objects.filter(restaurant=self.request.tenant)
        qs = _branch_scoped(qs, self.request)
        if self.request.query_params.get("include_dismissed") != "true":
            qs = qs.filter(is_dismissed=False)
        return qs

    @action(detail=False, methods=["post"])
    def generate(self, request):
        try:
            insights = services.generate_ai_insights(
                request.tenant, branch=getattr(request.user, "branch", None)
            )
        except AIUnavailableError as e:
            return Response({"detail": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(AIInsightSerializer(insights, many=True).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"])
    def dismiss(self, request, pk=None):
        insight = self.get_object()
        insight.is_dismissed = True
        insight.dismissed_at = timezone.now()
        insight.save(update_fields=["is_dismissed", "dismissed_at"])
        return Response(AIInsightSerializer(insight).data)
