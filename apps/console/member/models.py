# apps/console/member/models.py
from datetime import timedelta

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.crypto import get_random_string

class CoreMember(models.Model):
    user = models.OneToOneField(User, related_name='member', on_delete=models.CASCADE)
    active_account = models.ForeignKey('CoreAccount', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='active_members')
    email_verified = models.BooleanField(default=False)
    verification_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    verification_token_created = models.DateTimeField(null=True, blank=True)
    password_reset_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    password_reset_token_created = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'core_member'
        verbose_name = _("Member")
        verbose_name_plural = _("Members")

    def __str__(self):
        return self.user.username

    def set_active_account(self, account):
        if account in self.accounts.all():
            self.active_account = account
            self.save()
            return True
        return False

    def generate_verification_token(self):
        self.verification_token = get_random_string(64)
        self.verification_token_created = timezone.now()
        self.save()
        return self.verification_token

    def generate_password_reset_token(self):
        self.password_reset_token = get_random_string(64)
        self.password_reset_token_created = timezone.now()
        self.save()
        return self.password_reset_token

    def is_password_reset_token_valid(self):
        if not self.password_reset_token or not self.password_reset_token_created:
            return False

        expiry_time = self.password_reset_token_created + timedelta(minutes=15)
        return timezone.now() <= expiry_time