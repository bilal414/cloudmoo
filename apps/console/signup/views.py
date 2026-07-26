from django.views import View
from django.views.generic import CreateView, TemplateView
from django.urls import reverse_lazy
from django.contrib.auth import login
from django.db import transaction
from django.shortcuts import redirect
from django.contrib import messages
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from .forms import SignupForm
from ..account.models import CoreAccount, CoreAccountMembership
from ..member.models import CoreMember
from ..plan.models import CorePlan
from ..utils.email import EmailSender


class SignupView(CreateView):
    form_class = SignupForm
    template_name = 'console/signup/index.html'
    success_url = reverse_lazy('console:verification_sent')

    def dispatch(self, request, *args, **kwargs):
        if self.request.user.is_authenticated:
            return redirect('console:home:index')

        if not settings.REGISTRATION_OPEN:
            messages.error(
                request,
                "Registration is currently closed on this instance. "
                "Please contact the instance administrator."
            )
            return redirect('console:login')

        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        with transaction.atomic():
            user = form.save()
            user.is_active = True
            user.save()

            member = CoreMember.objects.create(user=user)

            account = CoreAccount.objects.create(
                name="Default",
                owner=user,
                is_default=True,
                status=CoreAccount.Status.INACTIVE,
                plan=CorePlan.get_default(),
            )

            # Create account membership with OWNER role
            CoreAccountMembership.objects.create(
                account=account,
                member=member,
                role=CoreAccountMembership.Role.OWNER
            )

            verification_token = member.generate_verification_token()
            self.send_verification_email(user, verification_token)

            member.active_account = account
            member.save()

        return super().form_valid(form)

    def send_verification_email(self, user, verification_token):
        verification_url = f"{settings.APP_URL}/verify-email/{verification_token}/"

        context = {
            'user': user,
            'verification_url': verification_url,
        }

        html_message = render_to_string('console/email/verify_email.html', context)
        plain_message = render_to_string('console/email/verify_email.txt', context)

        email_sender = EmailSender()
        success, message = email_sender.send_email(
            subject='Verify your CloudMoo email address',
            body_html=html_message,
            body_text=plain_message,
            recipient_list=[user.email]
        )

        if not success:
            messages.warning(
                self.request,
                'There was an issue sending the verification email. '
                'Please check the email configuration of this instance.'
            )


class VerifyEmailView(View):
    def get(self, request, token):
        try:
            member = CoreMember.objects.get(verification_token=token)

            # Check if token is expired (24 hours)
            token_age = timezone.now() - member.verification_token_created
            if token_age.total_seconds() > 86400:  # 24 hours
                messages.error(request, 'Verification link has expired. Please request a new one.')
                return redirect('console:login')

            member.email_verified = True
            member.verification_token = None
            member.verification_token_created = None
            member.save()

            member.user.is_active = True
            member.user.save()

            member.active_account.status = CoreAccount.Status.ACTIVE
            member.active_account.save()

            login(request, member.user)
            messages.success(request, 'Email verified successfully! Welcome to CloudMoo.')
            return redirect('console:home:index')

        except CoreMember.DoesNotExist:
            messages.error(request, 'Invalid verification link.')
            return redirect('console:login')


class VerificationSentView(TemplateView):
    template_name = 'console/signup/verification_sent.html'
