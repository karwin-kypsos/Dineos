import uuid
from decimal import Decimal

from django.db import models as dj_models
from django.utils import timezone
from rest_framework import generics, status, viewsets
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
    ApprovePurchaseOrderSerializer,
    ClosePurchaseOrderSerializer,
    GoodsReceiptCreateSerializer,
    GoodsReceiptSerializer,
    PurchaseOrderCreateSerializer,
    PurchaseOrderSerializer,
    RecipeItemSerializer,
    RecordWastageSerializer,
    StockAdditionSerializer,
)


def _branch_scoped(qs, request):
    # 2026-09-21, per Karwin: no branch-less-is-shared fallback, same
    # strictness the Menu endpoints took on 2026-09-14. An ingredient left
    # with branch=None predates Branch existing and was appearing in every
    # branch's stock list at once.
    branch = getattr(request.user, "branch", None)
    if branch is not None:
        qs = qs.filter(branch=branch)
    return qs


class IngredientViewSet(viewsets.ModelViewSet):
    serializer_class = IngredientSerializer

    def get_queryset(self):
        qs = Ingredient.objects.filter(restaurant=self.request.tenant, is_active=True)
        qs = _branch_scoped(qs, self.request)

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            try:
                uuid.UUID(branch_id)
                qs = qs.filter(branch_id=branch_id)
            except ValueError:
                pass  # malformed branch id — no filter applied, same convention as StaffViewSet

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
        # 2026-09-03 - a Manager (always pinned to one branch) can't
        # override it via the request body; Admin (no fixed branch) can
        # specify one explicitly (already tenant-validated by
        # IngredientSerializer.validate_branch) or omit it for a legacy/
        # restaurant-wide row. This used to always force the CALLER's own
        # branch (None for Admin), silently discarding whatever branch an
        # Admin actually specified.
        user_branch = getattr(self.request.user, "branch", None)
        branch = user_branch if user_branch is not None else serializer.validated_data.get("branch")
        serializer.save(restaurant=self.request.tenant, branch=branch)

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
            adjustment_reason=serializer.validated_data["adjustment_reason"],
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


class StockAdditionListView(generics.ListAPIView):
    """Audit trail of MANUAL stock additions (2026-09-22, per Karwin) -
    the mirror of the wastage log, pointing the other way.

    Deliberately excludes goods-receipt additions: those already have
    full traceability through the purchase order, and mixing them in
    would bury the hand-entered ones this exists to surface.

    HONEST CAVEAT, and it is in the docs too: rows created before
    2026-09-21 have a blank reason. Until then add_stock recorded no
    reason at all, and the same function served both the manual endpoint
    AND the old PO receive path - so for those historical rows there is
    no way to tell a manual correction from a PO delivery. They are
    included rather than hidden, because an audit list that silently
    drops history is worse than one that shows an unlabelled row. Filter
    ?reason= to see only the labelled ones.

    Paginated normally (count/next/previous/results), unlike the wastage
    log, which returns a single day's totals as one object.
    """

    serializer_class = StockAdditionSerializer
    permission_classes = [IsAdminOrManager]

    def get_queryset(self):
        qs = (
            StockMovement.objects
            .filter(
                ingredient__restaurant=self.request.tenant,
                movement_type=StockMovement.MovementType.RESTOCK,
            )
            .exclude(adjustment_reason=StockMovement.AdjustmentReason.GOODS_RECEIPT)
            .select_related("ingredient", "ingredient__branch", "recorded_by")
            .order_by("-recorded_at", "-id")
        )

        # Branch: implicit for a user pinned to one, explicit ?branch= for
        # an Admin (who has none) - same convention as the rest of this
        # module since 2026-09-21, no branch-less fallback.
        branch = getattr(self.request.user, "branch", None)
        if branch is not None:
            qs = qs.filter(ingredient__branch=branch)
        branch_id = self.request.query_params.get("branch")
        if branch_id:
            try:
                uuid.UUID(branch_id)
                qs = qs.filter(ingredient__branch_id=branch_id)
            except ValueError:
                pass  # malformed id ignored, same convention as elsewhere

        ingredient_id = self.request.query_params.get("ingredient")
        if ingredient_id:
            try:
                uuid.UUID(ingredient_id)
                qs = qs.filter(ingredient_id=ingredient_id)
            except ValueError:
                return qs.none()

        reason = self.request.query_params.get("reason", "").strip().upper()
        if reason in StockMovement.AdjustmentReason.values:
            qs = qs.filter(adjustment_reason=reason)

        # Inclusive calendar dates, same as the purchase-order history
        # range filter.
        date_from = self.request.query_params.get("date_from")
        if date_from:
            try:
                qs = qs.filter(
                    recorded_at__date__gte=timezone.datetime.strptime(date_from, "%Y-%m-%d").date()
                )
            except ValueError:
                pass
        date_to = self.request.query_params.get("date_to")
        if date_to:
            try:
                qs = qs.filter(
                    recorded_at__date__lte=timezone.datetime.strptime(date_to, "%Y-%m-%d").date()
                )
            except ValueError:
                pass
        return qs


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
            # Strict since 2026-09-21 — see _branch_scoped above.
            movements = movements.filter(ingredient__branch=branch)

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
            qs = qs.filter(status=PurchaseOrder.Status.PENDING_APPROVAL)

        is_emergency = self.request.query_params.get("is_emergency", "").strip().lower()
        if is_emergency == "true":
            qs = qs.filter(is_emergency=True)
        elif is_emergency == "false":
            qs = qs.filter(is_emergency=False)

        # 2026-09-21: the enum changed under existing clients, and an
        # unrecognised value here falls through and returns EVERY po -
        # so an app still sending "PENDING" would quietly get rejected
        # and received orders mixed into its pending list, which looks
        # like data rather than an error. Map the two renamed values so
        # an un-updated client keeps getting correct results while the
        # apps catch up.
        _LEGACY_STATUS = {
            "PENDING": PurchaseOrder.Status.PENDING_APPROVAL,
            "RECEIVED": PurchaseOrder.Status.FULLY_RECEIVED,
        }
        status_filter = self.request.query_params.get("status", "").strip().upper()
        status_filter = _LEGACY_STATUS.get(status_filter, status_filter)
        if status_filter in PurchaseOrder.Status.values:
            qs = qs.filter(status=status_filter)

        # date (2026-09-04, per Karwin - Admin's Purchase Orders list always
        # sends one selected branch + one selected date, defaulting to today
        # on the client side). A single exact-day filter on created_at,
        # distinct from the date_from/date_to range below.
        date_param = self.request.query_params.get("date")
        if date_param:
            try:
                target_date = timezone.datetime.strptime(date_param, "%Y-%m-%d").date()
                qs = qs.filter(created_at__date=target_date)
            except ValueError:
                pass  # malformed date — no filter applied, same convention as branch_id above

        # date_from/date_to (2026-08-27, per Shereena's Purchase Order History
        # screen needing a from/to range, same as the Billing dashboard and
        # bill history list). Both bounds are inclusive calendar dates,
        # filtered on created_at (when the PO was raised).
        date_from_param = self.request.query_params.get("date_from")
        date_to_param = self.request.query_params.get("date_to")
        if date_from_param:
            try:
                date_from = timezone.datetime.strptime(date_from_param, "%Y-%m-%d").date()
                qs = qs.filter(created_at__date__gte=date_from)
            except ValueError:
                pass  # malformed date — no filter applied, same convention as branch_id above
        if date_to_param:
            try:
                date_to = timezone.datetime.strptime(date_to_param, "%Y-%m-%d").date()
                qs = qs.filter(created_at__date__lte=date_to)
            except ValueError:
                pass

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
        """Approve, optionally cutting individual lines (2026-09-21).

        Body is optional: {"items": [{"item_id": 1, "approved_quantity":
        "5.00"}], "note": "supplier short on flour"}. Any line left out
        is approved at the full requested quantity, so an empty body is
        still a plain "approve the lot".
        """
        # get_object() first (same fix as OrderKitchenStatusView /
        # TableViewSet.override_status) - the service fetches by bare id
        # with no tenant check, so without this an Admin/Manager could
        # approve another restaurant's purchase order given its id.
        po_obj = self.get_object()
        serializer = ApprovePurchaseOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        by_id = {line.id: line for line in po_obj.lines.all()}
        items = []
        for entry in serializer.validated_data["items"]:
            line = by_id.get(entry["item_id"])
            if line is None:
                return Response(
                    {"items": f"Line {entry['item_id']} does not belong to this purchase order."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            items.append({"line": line, "approved_quantity": entry["approved_quantity"]})

        try:
            po = services.approve_purchase_order(
                po_obj.id, approved_by=request.user, items=items,
                note=serializer.validated_data["note"],
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
        return Response(PurchaseOrderSerializer(po).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        po_obj = self.get_object()
        try:
            po = services.reject_purchase_order(po_obj.id, rejected_by=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
        return Response(PurchaseOrderSerializer(po).data)

    @action(detail=True, methods=["post"], url_path="goods-receipts")
    def goods_receipts(self, request, pk=None):
        """Record one delivery. The ONLY endpoint that moves stock.

        {"items": [{"item_id": 1, "received_quantity": "3.00", "notes":
        ""}], "notes": "", "confirm_overdelivery": false}

        Receiving more than was approved returns 409 unless
        confirm_overdelivery is true - never silently clamped, never
        silently accepted.
        """
        po_obj = self.get_object()
        serializer = GoodsReceiptCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        by_id = {line.id: line for line in po_obj.lines.all()}
        items = []
        for entry in serializer.validated_data["items"]:
            line = by_id.get(entry["item_id"])
            if line is None:
                return Response(
                    {"items": f"Line {entry['item_id']} does not belong to this purchase order."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            items.append({
                "line": line,
                "received_quantity": entry["received_quantity"],
                "notes": entry["notes"],
            })

        try:
            receipt = services.record_goods_receipt(
                po_obj.id, items, received_by=request.user,
                confirm_overdelivery=serializer.validated_data["confirm_overdelivery"],
                notes=serializer.validated_data["notes"],
            )
        except services.OverDeliveryError as e:
            # over_delivered names the exact rows and amounts, so the UI
            # can highlight the offending line rather than parse `detail`.
            return Response(
                {"detail": str(e), "requires_confirmation": True, "over_delivered": e.lines},
                status=status.HTTP_409_CONFLICT,
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
        return Response(GoodsReceiptSerializer(receipt).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """Give up on the outstanding balance of a short-shipped PO."""
        po_obj = self.get_object()
        serializer = ClosePurchaseOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            po = services.close_purchase_order(
                po_obj.id, reason=serializer.validated_data["reason"], closed_by=request.user,
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_409_CONFLICT)
        return Response(PurchaseOrderSerializer(po).data)

    @action(detail=True, methods=["get"])
    def discrepancy(self, request, pk=None):
        """Ordered vs approved vs received per line - what to chase."""
        return Response(services.purchase_order_discrepancy(self.get_object()))


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
