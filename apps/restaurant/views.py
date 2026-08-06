from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAdmin

from . import services
from .models import Branch
from .serializers import BranchSerializer, RestaurantSettingsSerializer


class BranchViewSet(viewsets.ModelViewSet):
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
