import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import models as dj_models
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.ai_client import AIUnavailableError
from core.image_fields import ImageUploadErrorHandlingMixin
from core.permissions import IsAdminOrManager, IsAnyStaff
from core.tenancy import TenantObjectPermission, get_branch_from_table, get_tenant_from_table

from . import services
from .models import Category, MenuItem, PreparedPortion
from .serializers import (
    AddPortionsSerializer,
    CategoryCustomerSerializer,
    CategorySerializer,
    MenuItemCustomerSerializer,
    MenuItemSerializer,
    PreparedPortionSerializer,
)


def _effective_branch(request):
    """Explicit ?branch=<id> takes priority (e.g. an Org Admin viewing a
    specific branch's menu via the branch switcher — Admin has no
    user.branch of their own to fall back on); otherwise the caller's own
    fixed branch (Manager/Server/Cashier). Malformed/foreign ids are
    ignored rather than raising, same as List Staff's ?branch= filter."""
    branch_id = request.query_params.get("branch")
    if branch_id:
        try:
            uuid.UUID(branch_id)
        except ValueError:
            branch_id = None
        else:
            from apps.restaurant.models import Branch

            branch = Branch.objects.filter(id=branch_id, restaurant_id=request.tenant.id).first()
            if branch is not None:
                return branch
    return getattr(request.user, "branch", None)


def _apply_category_and_search(qs, request):
    """?category=<id> narrows to one category; ?search=<text> matches the
    dish name (case-insensitive substring). Malformed ?category= is ignored
    rather than raising, same convention as the branch/staff filters."""
    category_id = request.query_params.get("category")
    if category_id:
        try:
            category_id = int(category_id)
        except ValueError:
            category_id = None
        if category_id is not None:
            qs = qs.filter(category_id=category_id)

    search = request.query_params.get("search", "").strip()
    if search:
        qs = qs.filter(name__icontains=search)

    return qs


def _available_today_queryset(restaurant, branch=None):
    today_zero_ids = PreparedPortion.objects.filter(
        date=timezone.localdate(), portions_remaining=0
    ).values_list("menu_item_id", flat=True)
    qs = MenuItem.objects.filter(category__restaurant=restaurant, is_available=True, is_active=True)
    if branch is not None:
        # Branch-scoped categories only, plus any legacy restaurant-wide
        # (branch-less) categories that still apply to every branch.
        qs = qs.filter(dj_models.Q(category__branch=branch) | dj_models.Q(category__branch__isnull=True))
    return qs.exclude(id__in=today_zero_ids)


class CustomerMenuView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, table_id):
        restaurant = get_tenant_from_table(table_id)
        if restaurant is None:
            return Response({"detail": "Table not found."}, status=status.HTTP_404_NOT_FOUND)
        branch = get_branch_from_table(table_id)
        items = _apply_category_and_search(_available_today_queryset(restaurant, branch), request).select_related("category")
        return Response(MenuItemCustomerSerializer(items, many=True).data)


class CustomerCategoriesView(APIView):
    """GET /v1/menu/categories/customer/{table_id}/ (2026-08-25, per
    Shereena) — the public category-tab list for the Customer Web App.
    CategoryViewSet (list/retrieve) requires staff auth, which a customer
    scanning a QR code doesn't have, so this is a dedicated no-auth
    equivalent — same branch-scoping rule as the staff one (this branch's
    own categories, plus any legacy branch-less ones)."""

    permission_classes = [AllowAny]

    def get(self, request, table_id):
        restaurant = get_tenant_from_table(table_id)
        if restaurant is None:
            return Response({"detail": "Table not found."}, status=status.HTTP_404_NOT_FOUND)
        branch = get_branch_from_table(table_id)
        categories = Category.objects.filter(restaurant=restaurant, is_active=True)
        if branch is not None:
            categories = categories.filter(dj_models.Q(branch=branch) | dj_models.Q(branch__isnull=True))
        return Response(CategoryCustomerSerializer(categories, many=True).data)


class OrderTakingMenuView(ImageUploadErrorHandlingMixin, APIView):
    """GET here is the lean order-taking view (Server/Cashier); POST is
    Manager-only item creation — the doc specifies both on the same path.
    This is the real Create Menu Item path — MenuItemViewSet's own create
    action below is never wired to a URL (only list/retrieve/update/
    destroy are), so its ImageUploadErrorHandlingMixin covers Update Menu
    Item, not Create.
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminOrManager()]
        return [IsAnyStaff()]

    def get(self, request):
        items = _apply_category_and_search(
            _available_today_queryset(request.tenant, _effective_branch(request)), request
        ).select_related("category")
        return Response(MenuItemCustomerSerializer(items, many=True).data)

    def post(self, request):
        serializer = MenuItemSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        # Category must belong to the caller's own restaurant — never trust
        # a client-supplied category id blindly (cross-tenant injection).
        category = serializer.validated_data["category"]
        if category.restaurant_id != request.tenant.id:
            return Response({"category": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MenuItemViewSet(ImageUploadErrorHandlingMixin, viewsets.ModelViewSet):
    """Manager/Admin management view — all items, including unavailable ones."""

    serializer_class = MenuItemSerializer

    def get_queryset(self):
        qs = MenuItem.objects.filter(category__restaurant=self.request.tenant).select_related("category")
        branch = _effective_branch(self.request)
        if branch is not None:
            qs = qs.filter(dj_models.Q(category__branch=branch) | dj_models.Q(category__branch__isnull=True))
        return _apply_category_and_search(qs, self.request)

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

    def destroy(self, request, *args, **kwargs):
        """Real delete when nothing references this item yet (a
        just-created dish with no order history) — but OrderItem.menu_item
        is on_delete=PROTECT (order history must stay intact), so any item
        that has ever been ordered can't actually be removed from the
        database. Rather than let that surface as an unhandled 500, fall
        back to the same soft-deactivate this item already supports for
        the customer-facing menu (is_active=False, same convention as
        Branch/Ingredient) — 'delete' on an in-use item just means 'stop
        showing it' in practice.
        """
        from django.db.models import ProtectedError

        item = self.get_object()
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            item.is_active = False
            item.save(update_fields=["is_active"])
            return Response(MenuItemSerializer(item).data)


class CategoryViewSet(ImageUploadErrorHandlingMixin, viewsets.ModelViewSet):
    serializer_class = CategorySerializer

    def get_queryset(self):
        qs = Category.objects.filter(restaurant=self.request.tenant)
        branch = _effective_branch(self.request)
        if branch is not None:
            qs = qs.filter(dj_models.Q(branch=branch) | dj_models.Q(branch__isnull=True))
        return qs

    def get_permissions(self):
        base = [IsAuthenticated()] if self.action in ("list", "retrieve") else [IsAdminOrManager()]
        return base + [TenantObjectPermission()]

    def perform_create(self, serializer):
        # 2026-09-03 - a Manager (always pinned to one branch) can't
        # override it via the request body; Admin (no fixed branch) can
        # specify one explicitly (already tenant-validated by
        # CategorySerializer.validate_branch) or omit it for a legacy/
        # restaurant-wide row. This used to always force the CALLER's own
        # branch (None for Admin), silently discarding whatever branch an
        # Admin actually specified.
        user_branch = getattr(self.request.user, "branch", None)
        branch = user_branch if user_branch is not None else serializer.validated_data.get("branch")
        serializer.save(restaurant=self.request.tenant, branch=branch)

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


class PrepForecastView(APIView):
    """Prep Log screens' 'AI Prep Forecast' — 'Based on last 4 Tuesdays:
    Chicken Biryani averaged 24 servings.' Stateless (recomputed fresh each
    call, not persisted) since it's a live planning aid for whichever date
    is being prepped for, not a dismissible alert. ?date=YYYY-MM-DD defaults
    to today; ?branch=<id> narrows to one branch; ?lookback=<n> overrides
    the default 4 occurrences.
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

        branch = None
        branch_id = request.query_params.get("branch")
        if branch_id:
            try:
                branch = request.tenant.branches.get(id=branch_id)
            except (ValueError, request.tenant.branches.model.DoesNotExist):
                return Response({"branch": "Branch not found."}, status=status.HTTP_404_NOT_FOUND)

        lookback = request.query_params.get("lookback")
        try:
            lookback = int(lookback) if lookback else 4
        except ValueError:
            return Response({"lookback": "Must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            forecasts, target_date = services.generate_prep_forecast(
                request.tenant, branch=branch, target_date=target_date, lookback_occurrences=lookback
            )
        except AIUnavailableError as e:
            return Response({"detail": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({
            "target_date": target_date.isoformat(),
            "weekday": target_date.strftime("%A"),
            "lookback_occurrences": lookback,
            "forecasts": forecasts,
        })


class AddPortionsView(APIView):
    permission_classes = [IsAdminOrManager]

    def patch(self, request, dish_id):
        serializer = AddPortionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # dish_id is a guessable sequential int PK — confirm tenant ownership
        # before mutating anything.
        get_object_or_404(MenuItem, id=dish_id, category__restaurant=request.tenant)

        overrides = serializer.validated_data.get("deduction_overrides")
        if overrides:
            from apps.inventory.models import Ingredient

            valid_ids = set(
                Ingredient.objects.filter(
                    id__in=[o["ingredient_id"] for o in overrides], restaurant=request.tenant
                ).values_list("id", flat=True)
            )
            if any(o["ingredient_id"] not in valid_ids for o in overrides):
                return Response({"deduction_overrides": "Ingredient not found."}, status=status.HTTP_404_NOT_FOUND)

        portion = services.add_portions(
            dish_id, serializer.validated_data["additional_quantity"],
            recorded_by=request.user, deduction_overrides=overrides,
        )

        from apps.notifications.services import notify_role

        notify_role(
            ["ADMIN", "MANAGER"], tenant=request.tenant, type="PREP_LOGGED",
            title="Daily prep logged",
            body=f"{serializer.validated_data['additional_quantity']} portions of {portion.menu_item.name} added.",
            data={"menu_item_id": portion.menu_item_id}, branch=portion.menu_item.category.branch,
        )

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
