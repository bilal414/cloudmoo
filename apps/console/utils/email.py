# apps/console/utils/email.py
import logging
from django.conf import settings
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


class EmailSender:
    """
    Sends transactional email through Django's configured EMAIL_BACKEND.

    Works with any Django email backend: SMTP (default), console (local
    development), django-ses, or any third-party backend.
    """

    def __init__(self):
        self.default_from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        if not self.default_from_email:
            raise ValueError("DEFAULT_FROM_EMAIL must be set in Django settings")

    def send_email(self, subject, body_html, body_text, recipient_list, from_email=None):
        if not from_email:
            from_email = self.default_from_email

        if not recipient_list:
            logger.error("No recipients provided")
            return False, "No recipients provided"

        try:
            message = EmailMultiAlternatives(
                subject=subject,
                body=body_text,
                from_email=from_email,
                to=recipient_list,
            )
            if body_html:
                message.attach_alternative(body_html, "text/html")
            message.send(fail_silently=False)

            logger.info(f"Successfully sent email to: {recipient_list}")
            return True, "Email sent"

        except Exception as e:
            logger.error(f"Unexpected error sending email: {str(e)}")
            logger.error(f"Attempted to send from: {from_email} to: {recipient_list}")
            return False, str(e)
