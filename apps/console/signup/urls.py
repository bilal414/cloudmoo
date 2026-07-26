from django.urls import path
from .views import SignupView, VerificationSentView, VerifyEmailView

urlpatterns = [
    path('signup/', SignupView.as_view(), name='signup'),
    path('verify-email/<str:token>/', VerifyEmailView.as_view(), name='verify_email'),
    path('verification-sent/', VerificationSentView.as_view(), name='verification_sent'),
]
