from collections import Counter

from django.db import models
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.kitchen.authentication import KDSKeyAuthentication
from core.permissions import IsAnyStaff, IsKDSDevice, IsServerOrKDSDevice
from core.tenancy import get_tenant_from_session

from . import services
from .models import Order
from .serializers import (
    KDSOrderSerializer,
    OrderCreateSerializer,
    OrderSerializer,
    OrderStatusUpdateSerializer,
    TakeawayOrderCreateSerializer,
)


def _request_branch(request):
    """The caller's branch, whether they're a staff JWT or a KDS device —
    KDS auth puts the device on request.auth, not request.user (see
    apps.kitchen.authentication.KDSKeyAuthentication)."""
    from apps.kitchen.models import KDSDevice

    if isinstance(request.auth, KDSDevice):
        return request.auth.branch
    return getattr(request.user, "branch", None)


class CreateOrderView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # A staff-authenticated caller (request.tenant resolved from their
        # JWT) can only place orders into their OWN restaurant's sessions —
        # otherwise they could write orders into another client's data using
        # a guessable/leaked session id.
        if request.tenant is not None and get_tenant_from_session(data["session_id"]) != request.tenant:
            raise PermissionDenied("This session does not belong to your restaurant.")

        items = [
            {"menu_item_id": item["menu_item"].id, "quantity": item["quantity"], "notes": item.get("notes", "")}
            for item in data["items"]
        ]
        placed_by = request.user if request.user and request.user.is_authenticated else None
        order = services.place_order(data["session_id"], items, placed_by=placed_by, notes=data.get("notes", ""))
        return Response(OrderSerializer(order).data, status=201)


class TakeawayOrderView(APIView):
    """Cashier's Take-away Order screen — no table/session, just a
    customer name/phone and the items, billed directly against the order
    once ready (see apps.billing.views.PayTakeawayBillView)."""

    permission_classes = [IsAnyStaff]

    def get(self, request):
        orders = (
            Order.objects.filter(order_type=Order.OrderType.TAKEAWAY, branch__restaurant=request.tenant)
            .prefetch_related("items")
            .order_by("-placed_at")
        )
        branch = getattr(request.user, "branch", None)
        if branch is not None:
            orders = orders.filter(branch=branch)

        status_filter = request.query_params.get("status", "").strip().upper()
        if status_filter and status_filter != "ALL" and status_filter in Order.Status.values:
            orders = orders.filter(status=status_filter)

        return Response(OrderSerializer(orders, many=True).data)

    def post(self, request):
        serializer = TakeawayOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        branch = getattr(request.user, "branch", None)
        if branch is None:
            raise PermissionDenied("Your staff account isn't assigned to a branch — takeaway orders need one.")

        items = [
            {"menu_item_id": item["menu_item"].id, "quantity": item["quantity"], "notes": item.get("notes", "")}
            for item in data["items"]
        ]
        order = services.place_takeaway_order(
            request.tenant, branch, items, customer_name=data["customer_name"],
            customer_phone=data["customer_phone"], placed_by=request.user, notes=data.get("notes", ""),
            existing_order_id=data.get("existing_order_id"),
        )
        return Response(OrderSerializer(order).data, status=201)


class TakeawayOrderDetailView(APIView):
    """Takeaway Order Details API (Shereena, 2026-08-22): the complete
    picture of one takeaway order across every round placed against it —
    round 1 plus every later round added via existing_order_id (see
    services.place_takeaway_order/takeaway_group) — since a single round's
    plain GET /v1/orders/{order_id}/ only shows that one round's items.
    order_id may be the root order's id or any later round's id; either way
    this resolves to the same group and returns it root-first."""

    permission_classes = [IsAnyStaff]

    def get(self, request, order_id):
        order = get_object_or_404(
            Order.objects.filter(order_type=Order.OrderType.TAKEAWAY, branch__restaurant=request.tenant),
            id=order_id,
        )
        root = order if order.parent_order_id is None else order.parent_order
        rounds = services.takeaway_group(
            Order.objects.select_related("branch").prefetch_related("items").get(id=root.id)
        )
        return Response({
            "order_id": str(root.id),
            "customer_name": root.customer_name,
            "customer_phone": root.customer_phone,
            "is_billed": hasattr(root, "takeaway_bill"),
            "rounds": OrderSerializer(rounds, many=True).data,
            "combined_total_amount": sum(
                (item.line_total for round_order in rounds for item in round_order.items.all()), 0
            ),
        })


class ActiveOrdersView(APIView):
    """KDS "Live" dashboard envelope (Shereena's KOT Live spec, 2026-08-21):
    server_time + summary status counts + the active order list, each order
    carrying pre-calculated card metrics (elapsed wait time, urgency, item
    breakdown) via KDSOrderSerializer. Used by both the Kitchen Display and
    the Server app; scoped to this one endpoint only — Ready Orders and
    every other endpoint keep their existing flat-array shape.

    Deliberately cheap: the active-orders queryset is materialized ONCE and
    reused for both the summary counts and the serialized order list (no
    second DB round-trip), same performance posture as the admin dashboard.
    """

    authentication_classes = [JWTAuthentication, KDSKeyAuthentication]
    permission_classes = [IsServerOrKDSDevice]

    def get(self, request):
        orders = (
            Order.objects.filter(
                models.Q(table__restaurant=request.tenant) | models.Q(branch__restaurant=request.tenant),
                status__in=["NEW", "ACCEPTED", "PREPARING"],
            )
            .select_related("table")
            .prefetch_related("items")
        )
        branch = _request_branch(request)
        if branch is not None:
            orders = orders.filter(models.Q(branch=branch) | models.Q(branch__isnull=True))

        orders = list(orders)
        status_counts = Counter(order.status for order in orders)

        return Response({
            "server_time": timezone.now(),
            "summary": {
                "total_orders_count": len(orders),
                "new_count": status_counts.get("NEW", 0),
                "accepted_count": status_counts.get("ACCEPTED", 0),
                "preparing_count": status_counts.get("PREPARING", 0),
                "ready_count": status_counts.get("READY", 0),
            },
            "orders": KDSOrderSerializer(orders, many=True).data,
        })


class ReadyOrdersView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        orders = (
            Order.objects.filter(
                models.Q(table__restaurant=request.tenant) | models.Q(branch__restaurant=request.tenant),
                status="READY",
            )
            .select_related("table")
            .prefetch_related("items")
        )
        branch = _request_branch(request)
        if branch is not None:
            orders = orders.filter(models.Q(branch=branch) | models.Q(branch__isnull=True))
        return Response(OrderSerializer(orders, many=True).data)


class MyOrdersView(APIView):
    """Server's own 'My Orders' screen — every order across every active
    status (NEW/ACCEPTED/PREPARING/READY) for just the tables assigned to
    the calling server, unlike Active/Ready Orders above which are
    branch-wide across every server. Dine-in only — assigned_server lives
    on TableSession, which takeaway orders don't have (round-robin
    assignment is dine-in only, see apps.tables.services.assign_next_server).
    """

    permission_classes = [IsAnyStaff]

    def get(self, request):
        orders = (
            Order.objects.filter(
                table__restaurant=request.tenant,
                session__assigned_server=request.user,
                status__in=["NEW", "ACCEPTED", "PREPARING", "READY"],
            )
            .select_related("table")
            .prefetch_related("items")
        )
        return Response(OrderSerializer(orders, many=True).data)


class OrderDetailView(APIView):
    authentication_classes = [JWTAuthentication, KDSKeyAuthentication]
    permission_classes = [IsServerOrKDSDevice]

    def get(self, request, order_id):
        order = (
            Order.objects.filter(models.Q(table__restaurant=request.tenant) | models.Q(branch__restaurant=request.tenant))
            .select_related("table")
            .prefetch_related("items")
            .get(id=order_id)
        )
        return Response(OrderSerializer(order).data)


class OrdersBySessionView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        # session_id is itself an unguessable UUID acting as the customer's
        # access token — no further tenant scoping needed to address it.
        orders = Order.objects.filter(session_id=session_id).order_by("round_number").prefetch_related("items")
        return Response(OrderSerializer(orders, many=True).data)


class OrdersByTableView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request, table_id):
        from django.utils import timezone

        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        orders = Order.objects.filter(
            table_id=table_id, table__restaurant=request.tenant, placed_at__gte=today_start
        ).prefetch_related("items")
        return Response(OrderSerializer(orders, many=True).data)


class OrderKitchenStatusView(APIView):
    authentication_classes = [KDSKeyAuthentication]
    permission_classes = [IsKDSDevice]

    def patch(self, request, order_id):
        serializer = OrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = services.advance_kitchen_status(order_id, serializer.validated_data["status"])
        return Response(OrderSerializer(order).data)


class OrderItemKitchenStatusView(APIView):
    """Per-item counterpart to OrderKitchenStatusView — lets the kitchen
    advance one item's status (e.g. one dish plated while another is still
    cooking) without touching its siblings or the whole order's status.
    Same auth/permission pattern as the whole-order endpoint. Tenant-scopes
    the order with the same dual-Q (dine-in table OR takeaway branch)
    pattern used by OrderDetailView, so an item under another restaurant's
    order 404s rather than leaking/mutating cross-tenant data.

    Auto-advance note (updated 2026-08-21): the whole order's own status
    auto-advances alongside item-level progress via this endpoint — see
    services._maybe_auto_advance_order. The moment the first item reaches
    PREPARING, the order advances ACCEPTED → PREPARING; once every item has
    independently reached READY, the order advances to READY too (unless
    already there or past it). Restaurants that never call this per-item
    endpoint are unaffected; the whole-order PATCH /v1/orders/{order_id}/status/
    endpoint still cascades every item forward in the other direction (see
    services._cascade_items_forward), so both flows stay in sync either way.
    """

    authentication_classes = [KDSKeyAuthentication]
    permission_classes = [IsKDSDevice]

    def patch(self, request, order_id, item_id):
        serializer = OrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        order = get_object_or_404(
            Order.objects.filter(
                models.Q(table__restaurant=request.tenant) | models.Q(branch__restaurant=request.tenant)
            ),
            id=order_id,
        )
        order, item = services.advance_item_kitchen_status(order, item_id, serializer.validated_data["status"])
        return Response(OrderSerializer(order).data)


class OrderCollectedView(APIView):
    permission_classes = [IsAnyStaff]

    def patch(self, request, order_id):
        order = services.mark_collected(order_id)
        return Response(OrderSerializer(order).data)


class OrderServedView(APIView):
    permission_classes = [IsAnyStaff]

    def patch(self, request, order_id):
        order = services.mark_served(order_id)
        return Response(OrderSerializer(order).data)
