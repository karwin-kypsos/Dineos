import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def send_notification_email(subject, body, to_email):
    """Best-effort email send — never raises, so a broken/unconfigured mail
    setup can't take down a login, invite, or password-reset request.
    A no-op until RESEND_API_KEY is set. TEST_EMAIL_OVERRIDE, if set,
    redirects every send to that one address regardless of to_email.

    Sends over HTTPS via Resend's API rather than raw SMTP — most PaaS
    hosts (Render included) block outbound SMTP ports entirely as an
    anti-spam measure, so a socket-based send there just hangs/fails
    with "Network is unreachable" regardless of credentials. HTTPS is
    never blocked (the app itself needs it to run)."""
    if not settings.RESEND_API_KEY:
        return

    recipient = settings.TEST_EMAIL_OVERRIDE or to_email
    try:
        response = httpx.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": settings.DEFAULT_FROM_EMAIL,
                "to": [recipient],
                "subject": subject,
                "text": body,
            },
            timeout=10,
        )
        response.raise_for_status()
    except Exception:
        logger.exception("Failed to send email to %s", recipient)
