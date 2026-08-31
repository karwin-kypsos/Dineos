from decimal import Decimal

from django.db.models import Q, Sum
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from core.image_fields import ImageUploadErrorHandlingMixin
from core.permissions import IsAdmin

from . import services
from .models import Branch
from .serializers import BranchSerializer, RestaurantSettingsSerializer


class BranchViewSet(ImageUploadErrorHandlingMixin, viewsets.ModelViewSet):
    """Admin-only branch management (list/create/update/deactivate),
    scoped to the calling Admin's own restaurant only.
    """

    serializer_class = BranchSerializer
    permission_classes = [IsAdmin]

    def get_queryset(self):
        qs = Branch.objects.filter(restaurant=self.request.user.restaurant)

        status_filter = self.request.query_params.get("status", "all")
        if status_filter == "active":
            qs = qs.filter(is_active=True)
        elif status_filter == "inactive":
            qs = qs.filter(is_active=False)
        # "all" (or anything else) — no status filter applied.

        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(name__icontains=search)

        return qs

    def list(self, request, *args, **kwargs):
        """Adds a restaurant-wide total_revenue/total_orders summary
        alongside the normal paginated branch list (2026-08-31): the
        Branches screen's "All Branches Overview" card was showing these
        as a hardcoded 0 — confirmed by checking the endpoint it actually
        calls (this one) had no such fields at all, so the frontend had
        nothing real to show. All-time across every branch (paid bills /
        non-cancelled orders), matching the plain "Total Revenue"/"Total
        Orders" wording here — distinct from the Admin Dashboard's
        "Today's Revenue", which is deliberately scoped to today only.
        """
        response = super().list(request, *args, **kwargs)

        from apps.billing.models import Bill
        from apps.orders.models import Order

        restaurant = request.user.restaurant
        total_revenue = Bill.objects.filter(
            Q(session__table__restaurant=restaurant) | Q(order__branch__restaurant=restaurant),
        ).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        total_orders = Order.objects.filter(
            Q(table__restaurant=restaurant) | Q(branch__restaurant=restaurant),
        ).exclude(status="CANCELLED").count()

        response.data["total_revenue"] = total_revenue
        response.data["total_orders"] = total_orders
        return response

    def perform_create(self, serializer):
        # Always the calling Admin's own restaurant — never client-supplied,
        # so one restaurant's Admin can never create a branch in another's.
        restaurant = self.request.user.restaurant
        max_branches = restaurant.max_branches
        if max_branches is not None and restaurant.branches.filter(is_active=True).count() >= max_branches:
            raise ValidationError(
                f"Your plan ({restaurant.get_plan_tier_display()}) allows up to {max_branches} branch(es)."
            )
        branch = serializer.save(restaurant=restaurant)
        if branch.table_count:
            services.sync_branch_tables(branch, branch.table_count)

    def perform_update(self, serializer):
        old_count = serializer.instance.table_count
        branch = serializer.save()
        if branch.table_count != old_count:
            services.sync_branch_tables(branch, branch.table_count)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active"])

    def destroy(self, request, *args, **kwargs):
        """Plain DELETE (no query params) is unchanged — soft-deactivate,
        same as always. ?permanent=true is a separate, deliberately
        two-step path for actually erasing a defunct/test branch: Table,
        TableSession, Order (both FKs), and Bill are all CASCADE from
        Table, so its tables' full order/bill history goes with it — this
        is NOT the same blast radius as the plain deactivate above, hence
        gating it behind its own flag rather than overloading ?confirm=true
        on the existing endpoint. Without ?confirm=true, nothing is
        deleted — just a 409 with the exact counts, same pattern as
        Delete Organization (apps.platform.views).
        """
        if request.query_params.get("permanent") != "true":
            return super().destroy(request, *args, **kwargs)

        from apps.orders.models import Order
        from apps.tables.models import Table, TableSession
        from apps.billing.models import Bill

        branch = self.get_object()
        tables = Table.objects.filter(branch=branch)

        if request.query_params.get("confirm") != "true":
            return Response(
                {
                    "detail": (
                        "This will permanently delete this branch's tables and everything tied to "
                        "them (sessions, orders, bills). Staff/ingredients/purchase orders under this "
                        "branch are kept, just unassigned from it. This cannot be undone. Resend this "
                        "request with ?permanent=true&confirm=true to proceed."
                    ),
                    "branch": branch.name,
                    "will_delete": {
                        "tables_count": tables.count(),
                        "table_sessions_count": TableSession.objects.filter(table__branch=branch).count(),
                        "orders_count": Order.objects.filter(table__branch=branch).count(),
                        "bills_count": Bill.objects.filter(session__table__branch=branch).count(),
                    },
                    "will_unassign": {
                        "staff_count": branch.staff.count(),
                        "ingredients_count": branch.ingredients.count(),
                        "purchase_orders_count": branch.purchase_orders.count(),
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )

        name = branch.name
        tables.delete()
        branch.delete()
        return Response({"detail": f"Branch '{name}' permanently deleted."}, status=status.HTTP_200_OK)


class RestaurantSettingsView(APIView):
    """The one slice of Restaurant an Admin can self-serve — GST and
    service charge rates. Everything else on the model stays Platform-only.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(RestaurantSettingsSerializer(request.user.restaurant).data)

    def patch(self, request):
        serializer = RestaurantSettingsSerializer(request.user.restaurant, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
