import pyotp
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.models import Group

from apps.console.login.models import CoreTOTPDevice
from apps.console.security.forms import PasswordChangeForm, SetupTOTPForm, DisableAppTwoFactorForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import FormView, TemplateView
from django.urls import reverse_lazy
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone


class PasswordChangeView(LoginRequiredMixin, FormView):
    template_name = 'console/security/password_change.html'
    form_class = PasswordChangeForm
    success_url = reverse_lazy('console:security:password_change')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.save()
        # Prevent the user from being logged out after password change
        update_session_auth_hash(self.request, form.user)
        messages.success(self.request, 'Your password was successfully updated!')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'Please correct the error below.')
        return super().form_invalid(form)


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'console/security/profile.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        member = user.member

        context.update({
            'first_name': user.first_name,
            'last_name': user.last_name,
            'email': user.email,
            'accounts': member.accounts.all(),
            'date_joined': user.date_joined,
            'last_login': user.last_login,
            'two_factor_enabled': user.groups.filter(name='two-factor-sms').exists(),
            'app_two_factor_enabled': user.groups.filter(name='two-factor-app').exists(),
        })
        return context


class AppTwoFactorView(LoginRequiredMixin, TemplateView):
    template_name = 'console/security/app_two_factor.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        context['has_totp'] = hasattr(user, 'totp_device') and user.totp_device.is_verified
        context['in_app_2fa_group'] = user.groups.filter(name='two-factor-app').exists()

        return context


class SetupAppTwoFactorView(LoginRequiredMixin, FormView):
    template_name = 'console/security/setup_app_two_factor.html'
    form_class = SetupTOTPForm
    success_url = reverse_lazy('console:security:app_two_factor')

    def dispatch(self, request, *args, **kwargs):
        if hasattr(request.user, 'totp_device') and request.user.totp_device.is_verified:
            messages.warning(request, "App-based two-factor authentication is already set up.")
            return redirect('console:security:app_two_factor')

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        device, _ = CoreTOTPDevice.objects.get_or_create(
            user=self.request.user,
            defaults={'secret_key': pyotp.random_base32()}
        )
        context['qr_code'] = device.get_qr_code()
        context['secret_key'] = device.secret_key
        return context

    def form_valid(self, form):
        device = self.request.user.totp_device
        token = form.cleaned_data['token']

        if device.verify_token(token):
            device.is_verified = True
            device.save()

            # Add user to two-factor-app group
            two_factor_group, _ = Group.objects.get_or_create(name='two-factor-app')
            self.request.user.groups.add(two_factor_group)

            messages.success(self.request, "App-based two-factor authentication has been set up successfully.")
            return super().form_valid(form)

        form.add_error('token', "Invalid verification code")
        return self.form_invalid(form)


class DisableAppTwoFactorView(LoginRequiredMixin, FormView):
    template_name = 'console/security/disable_app_two_factor.html'
    form_class = DisableAppTwoFactorForm
    success_url = reverse_lazy('console:security:app_two_factor')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def dispatch(self, request, *args, **kwargs):
        if not self.request.user.groups.filter(name='two-factor-app').exists():
            messages.info(request, 'App-based two-factor authentication is not enabled.')
            return redirect('console:security:app_two_factor')
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        user = self.request.user

        # Remove from two-factor-app group
        two_factor_group = Group.objects.get(name='two-factor-app')
        user.groups.remove(two_factor_group)

        # Delete TOTP device
        if hasattr(user, 'totp_device'):
            user.totp_device.delete()

        messages.success(self.request, 'App-based two-factor authentication has been disabled.')
        return super().form_valid(form)

