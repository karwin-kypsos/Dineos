"""Subscription rules (2026-10-05, per Karwin - "set it up, we will decide
the price later"):

- Prices come from settings (PLAN_PRICE_<TIER>, rupees a month). Unset means
  checkout answers 409 and /v1/auth/plans/ shows price null.
- Upgrade: the new plan's features apply as soon as the mandate is
  authorised; its price starts when the period already paid for ends.
- Downgrade: applies when the period already paid for ends.
- A trial isn't charged: the first charge is on trial_ends_at.
- Cancel: renewals stop, access lasts to the end of the paid period, then the
  restaurant is PAYMENT_DUE - its Admin can still sign in and resubscribe,
  everyone else is locked out (core.tenancy).
- Razorpay's webhook is the source of truth; the app's verify call only
  speeds things up. Every transition here is idempotent, because both arrive.
"""
import datetime
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core import razorpay_client
from core.razorpay_client import RazorpayUnavailableError

from .models import TIER_RANK, RazorpayPlan, Subscription, SubscriptionPayment

logger = logging.getLogger(__name__)

# Statuses in which a subscription is the restaurant's current one.
LIVE = (Subscription.Status.AUTHENTICATED, Subscription.Status.ACTIVE, Subscription.Status.PENDING)
# 10 years of monthly charges - Razorpay requires a finite count.
TOTAL_COUNT = 120


class PricesNotSet(Exception):
    pass


class AlreadyOnPlan(Exception):
    pass


class NoSubscription(Exception):
    pass


def plan_price(tier):
    """Monthly price in rupees as a Decimal, or None while unset."""
    raw = str(getattr(settings, "PLAN_PRICES", {}).get(tier) or "").strip()
    if not raw:
        return None
    try:
        price = Decimal(raw)
    except Exception:
        logger.error("PLAN_PRICE_%s is not a number: %r", tier, raw)
        return None
    return price if price > 0 else None


def _aware(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), tz=datetime.timezone.utc) if epoch else None


def current_subscription(restaurant):
    """The subscription whose plan the restaurant is on now. During a
    scheduled downgrade that is the old one, not the newer one waiting for
    its start date (see pending_change)."""
    live = restaurant.subscriptions.filter(status__in=LIVE)
    return live.filter(plan_applied_at__isnull=False).order_by("-plan_applied_at").first() or live.order_by("-created_at").first()


def pending_change(restaurant):
    """A live subscription that hasn't taken effect yet (a downgrade waiting
    for the paid period to end), or None."""
    current = current_subscription(restaurant)
    if current is None:
        return None
    return restaurant.subscriptions.filter(status__in=LIVE, plan_applied_at__isnull=True).exclude(id=current.id).order_by("-created_at").first()


def razorpay_plan_for(tier, amount):
    key = settings.RAZORPAY_KEY_ID
    plan = RazorpayPlan.objects.filter(plan_tier=tier, billing_cycle="MONTHLY", amount=amount, razorpay_key_id=key).first()
    if plan is None:
        from apps.restaurant.models import Restaurant

        made = razorpay_client.create_plan(f"DineOS {Restaurant.PlanTier(tier).label} (monthly)", amount)
        plan, _ = RazorpayPlan.objects.get_or_create(
            plan_tier=tier, billing_cycle="MONTHLY", amount=amount, razorpay_key_id=key, defaults={"razorpay_plan_id": made["id"]},
        )
    return plan


def change_kind(current, tier):
    if current is None:
        return "new"
    return "upgrade" if TIER_RANK[tier] > TIER_RANK[current.plan_tier] else "downgrade"


@transaction.atomic
def start_checkout(restaurant, tier, user):
    """Returns (subscription, kind). Reuses an unfinished checkout for the
    same plan so a double tap doesn't make two Razorpay subscriptions."""
    from apps.restaurant.models import Restaurant

    price = plan_price(tier)
    if price is None:
        raise PricesNotSet()
    restaurant = Restaurant.objects.select_for_update().get(id=restaurant.id)
    current = current_subscription(restaurant)
    if current is not None and current.plan_tier == tier and not current.cancel_at_period_end:
        raise AlreadyOnPlan()
    kind = change_kind(current, tier)

    unfinished = restaurant.subscriptions.filter(
        status=Subscription.Status.CREATED, plan_tier=tier, amount=price, replaces=current,
        created_at__gte=timezone.now() - datetime.timedelta(days=1),
    ).first()
    if unfinished is not None:
        return unfinished, kind

    now = timezone.now()
    if current is not None and current.current_end and current.current_end > now:
        start_at = current.current_end
    elif current is not None and current.start_at and current.start_at > now:
        start_at = current.start_at  # replacing one whose billing hasn't started yet
    elif restaurant.status == Restaurant.Status.TRIAL and restaurant.trial_ends_at and restaurant.trial_ends_at > now:
        start_at = restaurant.trial_ends_at
    else:
        start_at = None

    plan = razorpay_plan_for(tier, price)
    made = razorpay_client.create_subscription(
        plan.razorpay_plan_id, TOTAL_COUNT, start_at=start_at,
        notes={"restaurant_id": str(restaurant.id), "plan_tier": tier},
    )
    sub = Subscription.objects.create(
        restaurant=restaurant, plan_tier=tier, amount=price, razorpay_plan_id=plan.razorpay_plan_id,
        razorpay_subscription_id=made["id"], start_at=start_at, replaces=current, created_by=user,
    )
    return sub, kind


def _apply_tier(sub, now):
    from apps.restaurant.plans import apply_plan_preset

    if sub.plan_applied_at is None:
        apply_plan_preset(sub.restaurant, sub.plan_tier)
        sub.plan_applied_at = now
        _log(sub.restaurant, "SUBSCRIPTION_PLAN_CHANGED", f"Now on the {sub.plan_tier.title()} plan (subscription)")


def _restore_access(restaurant):
    from apps.restaurant.models import Restaurant

    if restaurant.status == Restaurant.Status.PAYMENT_DUE:
        restaurant.status, restaurant.is_active = Restaurant.Status.ACTIVE, True
        restaurant.save(update_fields=["status", "is_active"])


def _log(restaurant, action, description):
    from apps.platform.models import PlatformActivityLog

    PlatformActivityLog.objects.create(actor=None, action=action, restaurant=restaurant, description=f"'{restaurant.name}': {description}")


def _lock(sub):
    return Subscription.objects.select_for_update(of=("self",)).select_related("restaurant", "replaces").get(id=sub.id)


@transaction.atomic
def mark_authorised(sub):
    """The mandate is approved (verify call or subscription.authenticated)."""
    sub = _lock(sub)
    now = timezone.now()
    first_time = sub.status == Subscription.Status.CREATED
    if first_time:
        sub.status = Subscription.Status.AUTHENTICATED
    old = sub.replaces
    if old is not None and old.status in LIVE and not old.cancel_at_period_end:
        try:
            razorpay_client.cancel_subscription(old.razorpay_subscription_id, at_cycle_end=old.status == Subscription.Status.ACTIVE)
        except RazorpayUnavailableError:
            logger.exception("Could not cancel replaced subscription %s", old.razorpay_subscription_id)
        old.cancel_at_period_end = True
        old.save(update_fields=["cancel_at_period_end", "updated_at"])
    kind = change_kind(old if old is not None and old.plan_applied_at else None, sub.plan_tier)
    # An upgrade (or a first plan) applies now; a downgrade waits for billing.
    if kind in ("new", "upgrade"):
        _apply_tier(sub, now)
        _restore_access(sub.restaurant)
    if first_time:
        _log(sub.restaurant, "SUBSCRIPTION_STARTED", f"{sub.plan_tier.title()} subscription authorised ({kind})")
    sub.save()
    return sub


@transaction.atomic
def mark_active(sub, entity=None):
    """Billing is running (subscription.activated / .charged)."""
    from apps.restaurant.models import Restaurant

    mark_authorised(sub)
    sub = _lock(sub)
    now = timezone.now()
    sub.status = Subscription.Status.ACTIVE
    if entity:
        sub.current_start = _aware(entity.get("current_start")) or sub.current_start
        sub.current_end = _aware(entity.get("current_end")) or sub.current_end
    _apply_tier(sub, now)  # a downgrade lands here, once its billing begins
    restaurant = sub.restaurant
    if restaurant.status in (Restaurant.Status.TRIAL, Restaurant.Status.PAYMENT_DUE):
        restaurant.status, restaurant.is_active = Restaurant.Status.ACTIVE, True
        restaurant.save(update_fields=["status", "is_active"])
    sub.save()
    return sub


@transaction.atomic
def record_payment(sub, payment, status):
    if not payment or not payment.get("id"):
        return None
    record, created = SubscriptionPayment.objects.get_or_create(
        razorpay_payment_id=payment["id"],
        defaults={
            "subscription": sub, "restaurant": sub.restaurant, "status": status,
            "amount": Decimal(payment.get("amount", 0)) / 100, "currency": payment.get("currency", "INR"),
            "method": payment.get("method") or "", "razorpay_invoice_id": payment.get("invoice_id") or "",
            "plan_tier": sub.plan_tier, "period_start": sub.current_start, "period_end": sub.current_end,
        },
    )
    if not created and record.status != status and status == SubscriptionPayment.Status.PAID:
        record.status = status
        record.save(update_fields=["status"])
    return record


@transaction.atomic
def mark_renewal_failed(sub, halted):
    from apps.restaurant.models import Restaurant

    sub = _lock(sub)
    sub.status = Subscription.Status.HALTED if halted else Subscription.Status.PENDING
    sub.save(update_fields=["status", "updated_at"])
    _log(sub.restaurant, "SUBSCRIPTION_PAYMENT_FAILED", "Renewals stopped after failed payments" if halted else "A renewal payment failed; Razorpay is retrying")
    if halted and current_subscription(sub.restaurant) is None:
        restaurant = sub.restaurant
        restaurant.status, restaurant.is_active = Restaurant.Status.PAYMENT_DUE, False
        restaurant.save(update_fields=["status", "is_active"])
    return sub


@transaction.atomic
def mark_ended(sub, completed=False):
    """subscription.cancelled / .completed: Razorpay sends this once the paid
    period is over (we always cancel at cycle end). The restaurant becomes
    PAYMENT_DUE unless another subscription has taken over."""
    from apps.restaurant.models import Restaurant

    sub = _lock(sub)
    if sub.status in (Subscription.Status.CANCELLED, Subscription.Status.COMPLETED):
        return sub
    sub.status = Subscription.Status.COMPLETED if completed else Subscription.Status.CANCELLED
    sub.ended_at = timezone.now()
    sub.save(update_fields=["status", "ended_at", "updated_at"])
    successor = sub.replaced_by.filter(status__in=LIVE).exists()
    restaurant = sub.restaurant
    in_trial = restaurant.status == Restaurant.Status.TRIAL and restaurant.trial_ends_at and restaurant.trial_ends_at > timezone.now()
    if not successor and not in_trial and current_subscription(restaurant) is None:
        restaurant.status, restaurant.is_active = Restaurant.Status.PAYMENT_DUE, False
        restaurant.save(update_fields=["status", "is_active"])
        _log(restaurant, "SUBSCRIPTION_ENDED", "Subscription ended; renewal needed to continue")
    return sub


def cancel(restaurant):
    """Stop renewing. Access lasts until the paid period ends. Anything
    authorised whose billing hasn't started yet (a trial's first charge, a
    scheduled downgrade) is cancelled outright - nothing was paid for it."""
    live = list(restaurant.subscriptions.filter(status__in=LIVE, cancel_at_period_end=False))
    if not live:
        raise NoSubscription()
    for sub in live:
        started = sub.status != Subscription.Status.AUTHENTICATED
        razorpay_client.cancel_subscription(sub.razorpay_subscription_id, at_cycle_end=started)
        with transaction.atomic():
            sub = _lock(sub)
            sub.cancel_at_period_end = True
            sub.save(update_fields=["cancel_at_period_end", "updated_at"])
    return current_subscription(restaurant)


def handle_webhook(event, payload):
    """Razorpay subscription.* events. Unknown subscriptions are ignored
    (another environment's, or one created outside DineOS)."""
    entity = ((payload.get("payload") or {}).get("subscription") or {}).get("entity") or {}
    sub = Subscription.objects.filter(razorpay_subscription_id=entity.get("id")).first()
    if sub is None:
        return "ignored"
    payment = ((payload.get("payload") or {}).get("payment") or {}).get("entity")
    if event == "subscription.authenticated":
        mark_authorised(sub)
    elif event in ("subscription.activated", "subscription.resumed"):
        mark_active(sub, entity)
    elif event == "subscription.charged":
        sub = mark_active(sub, entity)
        record_payment(sub, payment, SubscriptionPayment.Status.PAID)
    elif event == "subscription.pending":
        sub = mark_renewal_failed(sub, halted=False)
        record_payment(sub, payment, SubscriptionPayment.Status.FAILED)
    elif event == "subscription.halted":
        mark_renewal_failed(sub, halted=True)
    elif event == "subscription.cancelled":
        mark_ended(sub)
    elif event == "subscription.completed":
        mark_ended(sub, completed=True)
    else:
        return "ignored"
    return "processed"
