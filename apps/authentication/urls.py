from django.urls import path

from .views import (
    ChangePasswordView,
    ForgotPasswordView,
    LoginView,
    LogoutView,
    MeView,
    RefreshTokenView,
    ResetPasswordView,
    SelectBranchView,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="auth-login"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("reset-password/", ResetPasswordView.as_view(), name="auth-reset-password"),
    path("me/", MeView.as_view(), name="auth-me"),
    path("select-branch/", SelectBranchView.as_view(), name="auth-select-branch"),
    path("refresh-token/", RefreshTokenView.as_view(), name="auth-refresh-token"),
    path("change-password/", ChangePasswordView.as_view(), name="auth-change-password"),
]
