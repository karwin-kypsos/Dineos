from django.conf import settings
from django.contrib.auth import get_user_model
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
            if settings.DEBUG:
                response["token"] = reset_token.token
        return Response(response)


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reset_token = PasswordResetToken.objects.filter(token=serializer.validated_data["token"]).first()
        if not reset_token or not reset_token.is_valid:
            raise ValidationError({"token": "Invalid or expired reset token."})
        user = reset_token.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        from django.utils import timezone

        reset_token.used_at = timezone.now()
        reset_token.save(update_fields=["used_at"])
        return Response({"detail": "Password has been reset."})


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
    """Admin-only staff account management (list/create/update/deactivate)."""

    queryset = User.objects.all()
    permission_classes = [IsAdmin]

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        return UserSerializer

    @action(detail=True, methods=["patch"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        user.is_active = False
        user.save(update_fields=["is_active"])
        return Response(UserSerializer(user).data)
