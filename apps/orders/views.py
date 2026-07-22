from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.kitchen.authentication import KDSKeyAuthentication
from core.permissions import IsAnyStaff, IsKDSDevice, IsServerOrKDSDevice

from . import services
from .models import Order
from .serializers import OrderCreateSerializer, OrderSerializer, OrderStatusUpdateSerializer


class CreateOrderView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = OrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        items = [
            {"menu_item_id": item["menu_item"].id, "quantity": item["quantity"], "notes": item.get("notes", "")}
            for item in data["items"]
        ]
        placed_by = request.user if request.user and request.user.is_authenticated else None
        order = services.place_order(data["session_id"], items, placed_by=placed_by, notes=data.get("notes", ""))
        return Response(OrderSerializer(order).data, status=201)


class ActiveOrdersView(APIView):
    authentication_classes = [JWTAuthentication, KDSKeyAuthentication]
    permission_classes = [IsServerOrKDSDevice]

    def get(self, request):
        orders = Order.objects.filter(status__in=["NEW", "ACCEPTED", "PREPARING"]).select_related("table").prefetch_related("items")
        return Response(OrderSerializer(orders, many=True).data)


class ReadyOrdersView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        orders = Order.objects.filter(status="READY").select_related("table").prefetch_related("items")
        return Response(OrderSerializer(orders, many=True).data)


class OrderDetailView(APIView):
    authentication_classes = [JWTAuthentication, KDSKeyAuthentication]
    permission_classes = [IsServerOrKDSDevice]

    def get(self, request, order_id):
        order = Order.objects.select_related("table").prefetch_related("items").get(id=order_id)
        return Response(OrderSerializer(order).data)


class OrdersBySessionView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        orders = Order.objects.filter(session_id=session_id).order_by("round_number").prefetch_related("items")
        return Response(OrderSerializer(orders, many=True).data)


class OrdersByTableView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request, table_id):
        from django.utils import timezone

        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        orders = Order.objects.filter(table_id=table_id, placed_at__gte=today_start).prefetch_related("items")
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
