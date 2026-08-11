import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_notification_email(subject, body, to_email):
    """Best-effort email send — never raises, so a broken/unconfigured mail
    setup can't take down a login, invite, or password-reset request.
    A no-op until EMAIL_HOST_USER is set. TEST_EMAIL_OVERRIDE, if set,
    redirects every send to that one address regardless of to_email."""
    if not settings.EMAIL_HOST_USER:
        return

    recipient = settings.TEST_EMAIL_OVERRIDE or to_email
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False)
    except Exception:
        logger.exception("Failed to send email to %s", recipient)
