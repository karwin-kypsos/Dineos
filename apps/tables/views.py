from django.db import models
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
    serializer_class = TableSerializer
    permission_classes = [IsAnyStaff]

    def get_queryset(self):
        qs = Table.objects.filter(is_active=True)
        tenant = getattr(self.request, "tenant", None)
        # Only staff/KDS requests resolve a tenant via the middleware; the
        # login-less customer realm (retrieve/qr/session/etc.) addresses a
        # specific table by its own unguessable UUID or by
        # (restaurant_slug, table_number) together, so no tenant to filter by.
        if tenant is not None:
            qs = qs.filter(restaurant=tenant)
            # Staff requests scope further to their own branch once they
            # have one; branch-less legacy staff/tables still see everything
            # restaurant-wide.
            user_branch_id = getattr(self.request.user, "branch_id", None)
            if user_branch_id is not None:
                qs = qs.filter(models.Q(branch_id=user_branch_id) | models.Q(branch__isnull=True))
        return qs

    def get_permissions(self):
        # Note: self.action for a `.mapping`-based multi-method action is the
        # HANDLER FUNCTION NAME for that HTTP verb, not the shared url_path —
        # "session" (GET), "start_session" (POST), "close_session_view" (DELETE).
        if self.action in ("qr", "qr_branch", "retrieve", "bill_request", "session", "start_session"):
            return [AllowAny()]
        if self.action == "close_session_view":
            # Manual close without payment is a staff-only override.
            return [IsAnyStaff()]
        return super().get_permissions()

    @action(
        detail=False,
        methods=["get"],
        url_path="qr/(?P<restaurant_slug>[^/]+)/(?P<table_number>[^/.]+)",
    )
    def qr(self, request, restaurant_slug=None, table_number=None):
        # Legacy 2-segment QR URL, kept for branch-less tables printed
        # before Branch existed. table_number alone is only unique WITHIN a
        # restaurant when the table has no branch — see the branch-scoped
        # `qr_branch` route below for the disambiguated, current format.
        table = Table.objects.filter(
            restaurant__slug=restaurant_slug, table_number=table_number,
            branch__isnull=True, is_active=True,
        ).first()
        if not table:
            return Response({"detail": "Table not found."}, status=status.HTTP_404_NOT_FOUND)
        active_session = table.sessions.filter(status__in=["ACTIVE", "BILL_REQUESTED"]).first()
        data = QRLandingSerializer(
            {"table": table, "active_session": active_session}
        ).data
        return Response(data)

    @action(
        detail=False,
        methods=["get"],
        url_path="qr/(?P<restaurant_slug>[^/]+)/(?P<branch_slug>[^/]+)/(?P<table_number>[^/.]+)",
        url_name="qr-branch",
    )
    def qr_branch(self, request, restaurant_slug=None, branch_slug=None, table_number=None):
        # Current QR format: org_slug/branch_slug/table_number. table_number
        # is only unique WITHIN a branch, so the branch slug disambiguates
        # two branches of the same restaurant both having a "Table 5".
        table = Table.objects.filter(
            restaurant__slug=restaurant_slug, branch__slug=branch_slug,
            table_number=table_number, is_active=True,
        ).first()
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
