from django.db import models
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
        )
        return Response(OrderSerializer(order).data, status=201)


class ActiveOrdersView(APIView):
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
        return Response(OrderSerializer(orders, many=True).data)


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


class OrderDetailView(APIView):
    authentication_classes = [JWTAuthentication, KDSKeyAuthentication]
    permission_classes = [IsServerOrKDSDevice]

    def get(self, request, order_id):
        order = (
            Order.objects.filter(table__restaurant=request.tenant)
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
