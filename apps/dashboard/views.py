from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Bill
from apps.inventory.models import Ingredient, PurchaseOrder
from apps.orders.models import Order
from apps.tables.models import Table
from core.ai_client import AIUnavailableError
from core.permissions import IsAdminOrManager

from . import services
from .models import ChatMessage
from .serializers import ChatMessageSerializer, SendChatMessageSerializer


class AdminDashboardView(APIView):
    """Restaurant-level landing dashboard for Admin/Manager — the tenant's
    own view, distinct from the Super Admin's cross-tenant platform
    dashboard (apps.platform.views.DashboardView). Optionally narrowed to
    one branch via ?branch=<id>; defaults to the whole restaurant.
    """

    permission_classes = [IsAdminOrManager]

    def get(self, request):
        restaurant = request.tenant
        branch_id = request.query_params.get("branch")

        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # An order/bill is scoped to this restaurant either via its table
        # (dine-in — always set, regardless of whether that table has a
        # branch assigned) or via branch (takeaway, which has no table).
        orders_today = Order.objects.filter(
            Q(table__restaurant=restaurant) | Q(branch__restaurant=restaurant), placed_at__gte=today_start,
        ).exclude(status="CANCELLED")
        bills_today = Bill.objects.filter(
            Q(session__table__restaurant=restaurant) | Q(order__branch__restaurant=restaurant),
            paid_at__gte=today_start,
        )
        tables = Table.objects.filter(restaurant=restaurant, is_active=True)
        ingredients = Ingredient.objects.filter(restaurant=restaurant, is_active=True)
        pending_pos = PurchaseOrder.objects.filter(restaurant=restaurant, status=PurchaseOrder.Status.PENDING)

        if branch_id:
            # Same table-or-branch fallback as above — a dine-in order
            # whose table has this branch counts, even if the order row
            # itself predates branch scoping and has branch=None.
            orders_today = orders_today.filter(Q(table__branch_id=branch_id) | Q(branch_id=branch_id))
            bills_today = bills_today.filter(
                Q(session__table__branch_id=branch_id) | Q(order__branch_id=branch_id)
            )
            tables = tables.filter(branch_id=branch_id)
            ingredients = ingredients.filter(branch_id=branch_id)
            pending_pos = pending_pos.filter(branch_id=branch_id)

        today_revenue = bills_today.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        low_stock_count = sum(1 for ingredient in ingredients if ingredient.is_low_stock)

        return Response({
            "today_orders_count": orders_today.count(),
            "today_revenue": today_revenue,
            "today_bills_count": bills_today.count(),
            "active_tables_count": tables.exclude(status="AVAILABLE").count(),
            "total_tables_count": tables.count(),
            "low_stock_count": low_stock_count,
            "pending_purchase_orders_count": pending_pos.count(),
        })


class EndOfDayReviewView(APIView):
    """Manager's End of Day Review — the fuller picture the build guide
    calls out as blocked on Inventory existing (it now does): revenue (via
    the existing apps.billing.services.daily_collections, reused rather
    than duplicated), plus wastage, restocks, low-stock state, and staff
    activity for the day. Defaults to today; ?date=YYYY-MM-DD for a past day.
    """

    permission_classes = [IsAdminOrManager]

    def get(self, request):
        from datetime import datetime

        date_param = request.query_params.get("date")
        if date_param:
            try:
                review_date = datetime.strptime(date_param, "%Y-%m-%d").date()
            except ValueError:
                return Response({"date": "Expected format YYYY-MM-DD."}, status=400)
        else:
            review_date = timezone.localdate()

        return Response(services.compute_eod_data(request.tenant, review_date))


class LowStockAlertsView(APIView):
    """Rule-based (not ML) stock-out prediction, per the build guide's own
    example: days_remaining = current_stock / average_daily_usage over the
    trailing week. Swappable for a real model later without changing what
    this endpoint returns.
    """

    permission_classes = [IsAdminOrManager]

    LOOKBACK_DAYS = 7
    CRITICAL_DAYS = 1
    WARNING_DAYS = 3

    def get(self, request):
        from apps.inventory.models import Ingredient, StockMovement

        restaurant = request.tenant
        since = timezone.now() - timezone.timedelta(days=self.LOOKBACK_DAYS)

        alerts = []
        for ingredient in Ingredient.objects.filter(restaurant=restaurant, is_active=True):
            usage_total = StockMovement.objects.filter(
                ingredient=ingredient, movement_type=StockMovement.MovementType.USAGE, recorded_at__gte=since,
            ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
            avg_daily_usage = usage_total / self.LOOKBACK_DAYS

            if avg_daily_usage <= 0:
                days_remaining = None
                severity = "OK"
            else:
                days_remaining = float(ingredient.current_stock / avg_daily_usage)
                if days_remaining < self.CRITICAL_DAYS:
                    severity = "CRITICAL"
                elif days_remaining < self.WARNING_DAYS:
                    severity = "WARNING"
                else:
                    severity = "OK"

            if severity != "OK" or ingredient.is_low_stock:
                alerts.append({
                    "ingredient_id": str(ingredient.id),
                    "name": ingredient.name,
                    "unit": ingredient.unit,
                    "current_stock": ingredient.current_stock,
                    "avg_daily_usage": avg_daily_usage,
                    "days_remaining": days_remaining,
                    "severity": severity,
                })

        severity_order = {"CRITICAL": 0, "WARNING": 1, "OK": 2}
        alerts.sort(key=lambda a: severity_order[a["severity"]])
        return Response(alerts)


class ChatMessagesView(APIView):
    """AI Chat screen — 'Ask AI anything...'. GET returns the caller's own
    conversation history (oldest first); POST sends a new message and
    returns both it and the assistant's reply. One thread per user, not
    shared across the restaurant's team.
    """

    permission_classes = [IsAdminOrManager]

    def get(self, request):
        messages = ChatMessage.objects.filter(restaurant=request.tenant, user=request.user)
        return Response(ChatMessageSerializer(messages, many=True).data)

    def post(self, request):
        serializer = SendChatMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user_message, assistant_message = services.send_chat_message(
                request.tenant, request.user, serializer.validated_data["content"]
            )
        except AIUnavailableError as e:
            return Response({"detail": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(
            {
                "message": ChatMessageSerializer(user_message).data,
                "reply": ChatMessageSerializer(assistant_message).data,
            },
            status=status.HTTP_201_CREATED,
        )


class AIEndOfDayReportView(APIView):
    """AI End of Day Report — same underlying numbers as the manual End of
    Day Review (services.compute_eod_data), plus a Groq-phrased summary
    and 'Recommendations for Tomorrow' drawn from tomorrow's AI Prep
    Forecast and today's low/critical stock. Defaults to today;
    ?date=YYYY-MM-DD for a past day. Stateless — recomputed fresh each call.
    """

    permission_classes = [IsAdminOrManager]

    def get(self, request):
        from datetime import datetime

        date_param = request.query_params.get("date")
        if date_param:
            try:
                review_date = datetime.strptime(date_param, "%Y-%m-%d").date()
            except ValueError:
                return Response({"date": "Expected format YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            review_date = timezone.localdate()

        try:
            report = services.generate_eod_report(request.tenant, review_date)
        except AIUnavailableError as e:
            return Response({"detail": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(report)
