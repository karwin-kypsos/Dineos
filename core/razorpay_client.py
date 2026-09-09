import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class RazorpayUnavailableError(Exception):
    """Raised when Razorpay isn't configured on this platform yet, the
    restaurant hasn't linked an account, the API call itself fails, or a
    webhook's signature doesn't verify — callers turn this into a clean
    error response rather than a raw 500."""


def create_order(amount_rupees, receipt, linked_account_id=None):
    """One Razorpay Order per payment attempt. When linked_account_id is
    set (Restaurant.razorpay_account_id — Razorpay Route), the order
    routes straight to that restaurant's own linked account so their money
    settles to their own bank account, not a shared pool. When it's blank
    (2026-09-09: this platform's Razorpay account doesn't have Route
    enabled yet — "Route feature not enabled for the merchant", confirmed
    live against Razorpay's real API — Route needs Razorpay's own
    activation on the account, nothing on our side can turn it on), the
    order is created plainly with no transfers, so payment collection
    still works end to end today — it just settles to the platform's own
    account until Route gets approved and a restaurant links an account,
    at which point routing kicks in automatically with no code change."""
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        raise RazorpayUnavailableError("Razorpay is not configured on this platform yet.")

    import razorpay

    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
    amount_paise = int(amount_rupees * 100)
    order_payload = {"amount": amount_paise, "currency": "INR", "receipt": receipt}
    if linked_account_id:
        order_payload["transfers"] = [{"account": linked_account_id, "amount": amount_paise, "currency": "INR"}]
    try:
        return client.order.create(order_payload)
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
