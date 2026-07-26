from model_utils.models import TimeStampedModel
from django.contrib.auth.models import User
from django.db import models
from django.conf import settings
import pyotp
import qrcode
import io
import base64


class CoreTOTPDevice(TimeStampedModel):
    user = models.OneToOneField(User, related_name='totp_device', on_delete=models.CASCADE)
    secret_key = models.CharField(max_length=32)
    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"TOTP Device for {self.user.email}"

    def get_totp(self):
        return pyotp.TOTP(self.secret_key)

    def verify_token(self, token):
        totp = self.get_totp()
        return totp.verify(token)

    def get_qr_code(self):
        # Generate QR code for the TOTP secret
        totp = self.get_totp()
        provisioning_uri = totp.provisioning_uri(
            self.user.email,
            issuer_name=settings.COMPANY_NAME
        )

        # Create QR code image
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(provisioning_uri)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        # Convert to base64
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        return base64.b64encode(buffer.getvalue()).decode()

    @classmethod
    def create_device(cls, user):
        secret_key = pyotp.random_base32()
        device = cls.objects.create(
            user=user,
            secret_key=secret_key
        )
        return device

    class Meta:
        db_table = 'core_totp_device'
        verbose_name = "TOTP Device"
        verbose_name_plural = "TOTP Devices"
