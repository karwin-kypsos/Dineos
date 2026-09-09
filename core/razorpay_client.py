import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class RazorpayUnavailableError(Exception):
    """Raised when Razorpay isn't configured on this platform yet, the
    restaurant hasn't linked an account, the API call itself fails, or a
    webhook's signature doesn't verify — callers turn this into a clean
    error response rather than a raw 500."""


def create_order(amount_rupees, receipt, linked_account_id):
    """One Razorpay Order per payment attempt, routed via Razorpay Route so
    the money settles to the restaurant's own linked account (not one
    shared platform pool) — see Restaurant.razorpay_account_id."""
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        raise RazorpayUnavailableError("Razorpay is not configured on this platform yet.")

    import razorpay

    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    amount_paise = int(amount_rupees * 100)
    try:
        return client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt,
            "transfers": [{"account": linked_account_id, "amount": amount_paise, "currency": "INR"}],
        })
    except Exception as e:
        logger.exception("Razorpay order creation failed")
        raise RazorpayUnavailableError(f"Razorpay order creation failed: {e}") from e


def verify_webhook_signature(body, signature):
    """Raises RazorpayUnavailableError if the signature doesn't match — the
    webhook view turns this into a 400 rather than trusting an unverified
    payload. body is the raw request bytes/str exactly as received, not a
    re-serialized copy (the signature is computed over the exact bytes)."""
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        raise RazorpayUnavailableError("Razorpay webhook secret is not configured on this platform yet.")

    import razorpay
    from razorpay.errors import SignatureVerificationError

    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    try:
        client.utility.verify_webhook_signature(body, signature, settings.RAZORPAY_WEBHOOK_SECRET)
    except SignatureVerificationError as e:
        raise RazorpayUnavailableError(f"Webhook signature verification failed: {e}") from e
