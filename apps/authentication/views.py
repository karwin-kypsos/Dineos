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
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data["current_password"]):
            raise ValidationError({"current_password": "Current password is incorrect."})
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
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
        # Always the calling Admin's own restaurant — never client-supplied,
        # so one restaurant's Admin can never create a user in another's.
        user = serializer.save(restaurant=self.request.user.restaurant)

        invite_token = None
        if not request.data.get("password"):
            # No password supplied — this is an invite. There's no email
            # delivery wired up yet, so the token is handed back directly;
            # the caller (Admin) relays the accept-invite link themselves
            # until real email sending exists.
            from . import services

            invite_token = services.issue_invite(user).token

        data = UserSerializer(user).data
        if invite_token is not None:
            data["invite_token"] = invite_token
        return Response(data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        user.is_active = False
        user.save(update_fields=["is_active"])
        return Response(UserSerializer(user).data)
