from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAdminOrManager, IsAnyStaff

from . import services
from .models import Table, TableSession
from .serializers import (
    ManagerStatusOverrideSerializer,
    QRLandingSerializer,
    TableSerializer,
    TableSessionDetailSerializer,
    TableSessionSerializer,
)


class TableViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Table.objects.filter(is_active=True)
    serializer_class = TableSerializer
    permission_classes = [IsAnyStaff]

    def get_permissions(self):
        # Note: self.action for a `.mapping`-based multi-method action is the
        # HANDLER FUNCTION NAME for that HTTP verb, not the shared url_path —
        # "session" (GET), "start_session" (POST), "close_session_view" (DELETE).
        if self.action in ("qr", "retrieve", "bill_request", "session", "start_session"):
            return [AllowAny()]
        if self.action == "close_session_view":
            # Manual close without payment is a staff-only override.
            return [IsAnyStaff()]
        return super().get_permissions()

    @action(detail=False, methods=["get"], url_path="qr/(?P<table_number>[^/.]+)")
    def qr(self, request, table_number=None):
        table = Table.objects.filter(table_number=table_number, is_active=True).first()
        if not table:
            return Response({"detail": "Table not found."}, status=status.HTTP_404_NOT_FOUND)
        active_session = table.sessions.filter(status__in=["ACTIVE", "BILL_REQUESTED"]).first()
        data = QRLandingSerializer(
            {"table": table, "active_session": active_session}
        ).data
        return Response(data)

    @action(detail=True, methods=["get"])
    def session(self, request, pk=None):
        table = self.get_object()
        active_session = table.sessions.filter(status__in=["ACTIVE", "BILL_REQUESTED"]).first()
        if not active_session:
            return Response({"detail": "No active session for this table."}, status=status.HTTP_404_NOT_FOUND)
        return Response(TableSessionDetailSerializer(active_session).data)

    @session.mapping.post
    def start_session(self, request, pk=None):
        session, created = services.get_or_create_active_session(pk)
        return Response(
            TableSessionSerializer(session).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @session.mapping.delete
    def close_session_view(self, request, pk=None):
        table = self.get_object()
        active_session = table.sessions.filter(status__in=["ACTIVE", "BILL_REQUESTED"]).first()
        if not active_session:
            return Response({"detail": "No active session for this table."}, status=status.HTTP_404_NOT_FOUND)
        from apps.billing.models import Bill

        reason = TableSession.CloseReason.PAID if Bill.objects.filter(session=active_session).exists() else TableSession.CloseReason.MANAGER_OVERRIDE
        closed = services.close_session(active_session, reason=reason, closed_by=request.user)
        return Response(TableSessionSerializer(closed).data)

    @action(detail=True, methods=["post"], url_path="bill-request")
    def bill_request(self, request, pk=None):
        table = self.get_object()
        active_session = table.sessions.filter(status__in=["ACTIVE", "BILL_REQUESTED"]).first()
        if not active_session:
            return Response({"detail": "No active session for this table."}, status=status.HTTP_404_NOT_FOUND)
        session = services.request_bill(active_session.id)
        return Response(TableSessionSerializer(session).data)

    @action(detail=True, methods=["patch"], url_path="status", permission_classes=[IsAdminOrManager])
    def override_status(self, request, pk=None):
        serializer = ManagerStatusOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        table = services.manager_override_status(
            pk,
            status=serializer.validated_data["status"],
            mark_unpaid=serializer.validated_data["mark_unpaid"],
            manager=request.user,
        )
        return Response(TableSerializer(table).data)

    @action(detail=True, methods=["get"], permission_classes=[IsAdminOrManager])
    def history(self, request, pk=None):
        table = self.get_object()
        sessions = table.sessions.filter(status="CLOSED").order_by("-closed_at")[:50]
        return Response(TableSessionSerializer(sessions, many=True).data)
