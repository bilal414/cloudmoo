from django.urls import path
from .views import LoginView, ResendVerificationView, PasswordResetRequestView, PasswordResetConfirmView, \
    VerifyTwoFactorView

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('login/resend-verification/<str:email>/', ResendVerificationView.as_view(), name='resend_verification'),
    path('verify-2fa/', VerifyTwoFactorView.as_view(), name='verify-2fa'),
    path('password-reset/', PasswordResetRequestView.as_view(), name='password_reset_request'),
    path('password-reset/<str:token>/', PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
]
