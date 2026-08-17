import secrets
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from core.permissions import IsAdmin

from .models import PasswordResetToken
from .serializers import (
    ROLE_METADATA,
    ChangePasswordSerializer,
    DineOSTokenObtainPairSerializer,
    ForgotPasswordSerializer,
    MeSerializer,
    ResetPasswordSerializer,
    UserCreateSerializer,
    UserSerializer,
)

User = get_user_model()


class LoginView(TokenObtainPairView):
    serializer_class = DineOSTokenObtainPairSerializer


class RefreshTokenView(TokenRefreshView):
    pass


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh = request.data.get("refresh")
        if not refresh:
            raise ValidationError({"refresh": "This field is required."})
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email=serializer.validated_data["email"], is_active=True).first()
        response = {"detail": "If that email is registered, a reset link has been sent."}
        if user:
            reset_token = PasswordResetToken.issue(user)
            if settings.DEBUG or not settings.EMAIL_DELIVERY_ENABLED:
                response["token"] = reset_token.token
        return Response(response)


class ResetPasswordView(APIView):
    """Shared by both flows that end in 'now set a password': a genuine
    forgot-password reset, and completing an invite (apps.authentication.
    services.issue_invite locks the account and issues the same kind of
    token). Either way, a successful reset logs the user straight in —
    for an invite this is literally the "click the link, set a password,
    you're in" first-login step the product wants; for a reset it's a
    normal, common UX pattern.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reset_token = PasswordResetToken.objects.filter(token=serializer.validated_data["token"]).first()
        if not reset_token or not reset_token.is_valid:
            raise ValidationError({"token": "Invalid or expired reset token."})
        user = reset_token.user
        user.set_password(serializer.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        from django.utils import timezone

        reset_token.used_at = timezone.now()
        reset_token.save(update_fields=["used_at"])

        token = DineOSTokenObtainPairSerializer.get_token(user)
        return Response({
            "detail": "Password has been set.",
            "refresh": str(token),
            "access": str(token.access_token),
            "role": user.role,
            "role_id": ROLE_METADATA[user.role]["id"],
            "role_name": ROLE_METADATA[user.role]["name"],
            "name": user.name,
            "restaurant_id": str(user.restaurant_id),
        })


class MeView(generics.RetrieveAPIView):
    serializer_class = MeSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class SelectBranchView(APIView):
    """Persists the Org Admin's branch-switcher choice so it survives the
    login being skipped on later app opens (see resolve_branch_context in
    serializers.py, used by both /v1/auth/login/ and /v1/auth/me/).
    No-op-safe for Manager/Server/Cashier -- they're pinned to their own
    user.branch, which this never touches."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        from apps.restaurant.models import Branch

        branch_id = request.data.get("branch_id")
        if not branch_id:
            return Response({"branch_id": "This field is required."}, status=status.HTTP_400_BAD_REQUEST)

        branch = Branch.objects.filter(id=branch_id, restaurant_id=request.user.restaurant_id).first()
        if branch is None:
            return Response({"branch_id": "Branch not found."}, status=status.HTTP_404_NOT_FOUND)

        request.user.selected_branch = branch
        request.user.save(update_fields=["selected_branch"])
        return Response(MeSerializer(request.user).data)


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data["current_password"]):
            raise ValidationError({"current_password": "Current password is incorrect."})
        user.set_password(serializer.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        return Response({"detail": "Password changed."})


class StaffViewSet(viewsets.ModelViewSet):
    """Admin-only staff account management (list/create/update/deactivate),
    scoped to the calling Admin's own restaurant only.
    """

    serializer_class = UserSerializer
    permission_classes = [IsAdmin]

    def get_queryset(self):
        qs = User.objects.filter(restaurant=self.request.user.restaurant)

        branch_id = self.request.query_params.get("branch")
        if branch_id:
            try:
                uuid.UUID(branch_id)
                qs = qs.filter(branch_id=branch_id)
            except ValueError:
                pass  # malformed branch id — no filter applied, same as an unrecognized status filter elsewhere

        role = self.request.query_params.get("role", "").strip().upper()
        if role in User.Role.values:
            qs = qs.filter(role=role)

        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(email__icontains=search))

        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        return UserSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        temp_password = None
        if not request.data.get("password"):
            # No password supplied — the Add Staff form has no password
            # field at all; a human-chosen "temp password" tends to be
            # weak/guessable and there's no reason for Admin to invent one
            # anyway, so the backend generates a secure random one instead.
            temp_password = secrets.token_urlsafe(9)

        # Always the calling Admin's own restaurant — never client-supplied,
        # so one restaurant's Admin can never create a user in another's.
        user = serializer.save(
            restaurant=self.request.user.restaurant,
            **({"password": temp_password, "must_change_password": True} if temp_password else {}),
        )

        if temp_password is not None:
            from core.email import send_notification_email

            send_notification_email(
                subject="Your DineOS staff account",
                body=(
                    f"Hi {user.name or 'there'},\n\n"
                    f"An account has been created for you on DineOS. Log in with:\n\n"
                    f"Email: {user.email}\nTemporary password: {temp_password}\n\n"
                    f"You'll be asked to set your own password the first time you log in."
                ),
                to_email=user.email,
            )

        data = UserSerializer(user).data
        if temp_password is not None:
            data["temp_password"] = temp_password
        return Response(data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        user.is_active = False
        user.save(update_fields=["is_active"])
        return Response(UserSerializer(user).data)

    def destroy(self, request, *args, **kwargs):
        """Permanently deletes the account (name/email/role gone entirely —
        distinct from deactivate above, which just blocks login and keeps
        the record). Two-step like the Super Admin tenant delete: the first
        call (no ?confirm=true) deletes nothing and returns 409 describing
        who this would remove; only a second call with ?confirm=true
        actually performs it. Prefer deactivate for the normal 'remove this
        staff member' flow — this is for genuinely erasing a mistaken or
        duplicate account.
        """
        user = self.get_object()

        if request.query_params.get("confirm") != "true":
            return Response(
                {
                    "detail": (
                        "This permanently deletes this staff account — unlike deactivate, the record "
                        "itself is gone, not just blocked from logging in. This cannot be undone. Prefer "
                        "PATCH .../deactivate/ for the normal remove-a-staff-member flow. Resend this "
                        "request with ?confirm=true to proceed with permanent deletion."
                    ),
                    "staff_member": {"name": user.name, "email": user.email, "role": user.role},
                },
                status=status.HTTP_409_CONFLICT,
            )

        return super().destroy(request, *args, **kwargs)
