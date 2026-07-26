from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.auth.models import User
from django.shortcuts import redirect, get_object_or_404
from django.views.generic import FormView
from django.urls import reverse_lazy
from django.contrib import messages
from django.utils import timezone
from django.views import View

from .forms import LoginForm, PasswordResetRequestForm, PasswordResetConfirmForm, VerifyTwoFactorForm
from ..member.models import CoreMember
from ..utils.decorators import rate_limit
from ..utils.email import EmailSender
from django.template.loader import render_to_string


class EmailVerificationMixin:
    def send_verification_email(self, member):
        verification_url = self.request.build_absolute_uri(
            reverse_lazy('console:verify_email', kwargs={'token': member.verification_token})
        )

        context = {
            'user': member.user,
            'verification_url': verification_url,
        }

        html_message = render_to_string('console/email/verify_email.html', context)
        plain_message = render_to_string('console/email/verify_email.txt', context)

        email_sender = EmailSender()
        return email_sender.send_email(
            subject='Verify your CloudMoo email address',
            body_html=html_message,
            body_text=plain_message,
            recipient_list=[member.user.email]
        )


class LoginView(EmailVerificationMixin, FormView):
    template_name = 'console/login/index.html'
    form_class = LoginForm
    success_url = reverse_lazy('console:home:index')

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    @rate_limit('login', limit=10, period=900)
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        email = form.cleaned_data['email']
        password = form.cleaned_data['password']
        try:
            member = CoreMember.objects.get(user__email__iexact=email)
            user = authenticate(self.request, username=member.user.username, password=password)

            if user is not None:
                if not member.email_verified:
                    # Generate new verification token if needed
                    if not member.verification_token or \
                            (timezone.now() - member.verification_token_created).total_seconds() > 86400:
                        member.generate_verification_token()

                    context = {
                        'unverified_email': True,
                        'resend_url': reverse_lazy('console:resend_verification', kwargs={'email': email}),
                        'email': email
                    }
                    form.add_error(None, "Please verify your email address to continue")
                    return self.render_to_response(self.get_context_data(form=form, **context))

                if user.groups.filter(name='two-factor-app').exists():
                    # App-based 2FA logic
                    if not hasattr(user, 'totp_device') or not user.totp_device.is_verified:
                        form.add_error(None, "TOTP not set up. Please contact administrator.")
                        return self.form_invalid(form)

                    self.request.session['pending_user_id'] = user.id
                    self.request.session['two_factor_type'] = 'app'
                    return redirect('console:verify-2fa')

                login(self.request, user)
                return super().form_valid(form)
        except CoreMember.DoesNotExist:
            pass

        form.add_error(None, "Invalid email or password")
        return self.form_invalid(form)


class ResendVerificationView(EmailVerificationMixin, View):
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        email = kwargs.get('email')
        try:
            member = CoreMember.objects.get(user__email__iexact=email)

            # Generate new verification token if expired
            if not member.verification_token or \
                    (timezone.now() - member.verification_token_created).total_seconds() > 86400:
                member.generate_verification_token()

            success, message = self.send_verification_email(member)

            if success:
                messages.success(
                    request,
                    "Verification email has been resent. Please check your inbox."
                )
            else:
                messages.error(
                    request,
                    "Failed to send verification email. Please try again later."
                )

        except CoreMember.DoesNotExist:
            messages.error(request, "Email address not found.")

        return redirect('console:login')


class PasswordResetRequestView(FormView):
    template_name = 'console/login/password_reset_request.html'
    form_class = PasswordResetRequestForm
    success_url = reverse_lazy('console:password_reset_request')

    @rate_limit('password_reset', limit=3, period=900)
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        email = form.cleaned_data['email']
        try:
            member = CoreMember.objects.get(user__email__iexact=email)
            member.generate_password_reset_token()

            reset_url = self.request.build_absolute_uri(
                reverse_lazy('console:password_reset_confirm',
                             kwargs={'token': member.password_reset_token})
            )

            context = {
                'user': member.user,
                'reset_url': reset_url,
                'is_password_reset': True
            }

            html_message = render_to_string('console/email/password_reset.html', context)
            plain_message = render_to_string('console/email/password_reset.txt', context)

            email_sender = EmailSender()
            success, message = email_sender.send_email(
                subject='Reset your CloudMoo password',
                body_html=html_message,
                body_text=plain_message,
                recipient_list=[email]
            )

            if success:
                messages.success(
                    self.request,
                    "Password reset instructions have been sent to your email."
                )
            else:
                messages.error(
                    self.request,
                    "Failed to send password reset email. Please try again later."
                )

        except CoreMember.DoesNotExist:
            # Don't reveal whether the email exists
            messages.success(
                self.request,
                "If an account exists with this email, you will receive password reset instructions."
            )

        return super().form_valid(form)


class PasswordResetConfirmView(FormView):
    template_name = 'console/login/password_reset_confirm.html'
    form_class = PasswordResetConfirmForm
    success_url = reverse_lazy('console:login')

    def get_member(self):
        token = self.kwargs.get('token')
        return get_object_or_404(CoreMember, password_reset_token=token)

    def dispatch(self, request, *args, **kwargs):
        self.member = self.get_member()
        if not self.member.is_password_reset_token_valid():
            messages.error(request, "This password reset link has expired.")
            return redirect('console:password_reset_request')
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.member.user
        return kwargs

    def form_valid(self, form):
        form.save()
        self.member.password_reset_token = None
        self.member.password_reset_token_created = None
        self.member.save()
        messages.success(self.request, "Your password has been successfully reset.")
        return super().form_valid(form)


    def form_invalid(self, form):
        """
        Override to catch validation errors
        """
        # Make errors more visible to user
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(self.request, f"{error}")

        return super().form_invalid(form)

class VerifyTwoFactorView(FormView):
    template_name = 'console/login/verify_2fa.html'
    form_class = VerifyTwoFactorForm
    success_url = reverse_lazy('console:home:index')

    def dispatch(self, request, *args, **kwargs):
        if not request.session.get('pending_user_id'):
            return redirect('console:login')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['two_factor_type'] = self.request.session.get('two_factor_type', 'sms')
        return context

    def form_valid(self, form):
        user_id = self.request.session['pending_user_id']
        code = form.cleaned_data['code']
        two_factor_type = self.request.session.get('two_factor_type')

        try:
            user = User.objects.get(id=user_id)

            if not user.totp_device.verify_token(code):
                form.add_error('code', "Invalid verification code")
                return self.form_invalid(form)

            login(self.request, user)
            del self.request.session['pending_user_id']
            del self.request.session['two_factor_type']
            return super().form_valid(form)

        except User.DoesNotExist:
            form.add_error('code', "Invalid or expired verification code")
            return self.form_invalid(form)
