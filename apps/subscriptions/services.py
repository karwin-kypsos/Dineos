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
from django.db.models import Q
from django.utils import timezone

from core import razorpay_client
from core.razorpay_client import RazorpayUnavailableError

from .models import TIER_RANK, RazorpayPlan, Subscription, SubscriptionPayment

logger = logging.getLogger(__name__)

# Statuses in which a subscription is the restaurant's current one.
LIVE = (Subscription.Status.AUTHENTICATED, Subscription.Status.ACTIVE, Subscription.Status.PENDING)
# Final in Razorpay too: nothing brings these back, so a late or replayed
# event for one changes nothing (renewing is a new checkout).
ENDED = (Subscription.Status.CANCELLED, Subscription.Status.COMPLETED)
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
    return (
        restaurant.subscriptions.filter(status__in=LIVE, plan_applied_at__isnull=True, cancel_at_period_end=False)
        .exclude(id=current.id).order_by("-created_at").first()
    )


BILLING_STATUSES = ("trialing", "active", "past_due", "cancelled", "none")


def billing_status(restaurant):
    """The real billing state, from the subscriptions alone (2026-10-12, per
    Karwin - for the Super Admin app). Not from organization_status, which a
    Super Admin can set by hand (TRIAL / ACTIVE / SUSPENDED / PAYMENT_DUE):
      past_due  - a renewal is failing (Razorpay retrying), or the last
                  subscription stopped after its retries ran out
      active    - a paid billing period is running (or charged at checkout)
      trialing  - authorised, first charge still in the future (trial end)
      cancelled - the last subscription ended
      none      - never subscribed
    """
    live = list(restaurant.subscriptions.filter(status__in=LIVE))
    if any(s.status == Subscription.Status.PENDING for s in live):
        return "past_due"
    if any(s.status == Subscription.Status.ACTIVE for s in live):
        return "active"
    now = timezone.now()
    authorised = [s for s in live if s.status == Subscription.Status.AUTHENTICATED]
    if authorised:
        return "trialing" if all(s.start_at and s.start_at > now for s in authorised) else "active"
    last = restaurant.subscriptions.exclude(status=Subscription.Status.CREATED).order_by("-created_at").first()
    if last is None:
        return "none"
    return "past_due" if last.status == Subscription.Status.HALTED else "cancelled"


def billing_summary(restaurant):
    """subscription_status, next_billing_date and the last payment, for the
    Super Admin's organization responses."""
    sub = current_subscription(restaurant)
    last = restaurant.subscription_payments.order_by("-created_at").first()
    return {
        "subscription_status": billing_status(restaurant),
        # The next charge: the end of the paid period, or the first charge of
        # one not billing yet; null when renewals are stopped or nothing runs.
        "next_billing_date": None if (sub is None or sub.cancel_at_period_end) else (sub.current_end or sub.start_at),
        "last_payment_date": last.created_at if last else None,
        "last_payment_amount": str(last.amount.quantize(Decimal("0.01"))) if last else None,
        "last_payment_status": last.status if last else None,
    }


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


def change_kind(current, tier, on_plan=None):
    """new / upgrade / downgrade / renew. Judged against on_plan - the plan
    the restaurant is actually on, which a Super Admin can change by hand -
    rather than the subscription's own plan (2026-10-08: a restaurant moved
    to Starter by hand while its Growth subscription ran got 409
    already_on_plan for Growth)."""
    if current is None:
        return "new"
    on_plan = on_plan or current.plan_tier
    if tier == on_plan:
        # The same plan again after cancelling it: billing simply carries on
        # from the end of the period already paid for.
        return "renew"
    return "upgrade" if TIER_RANK[tier] > TIER_RANK[on_plan] else "downgrade"


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
    # Already on it = the restaurant has this plan AND a live subscription
    # paying for it that isn't being cancelled.
    if (current is not None and current.plan_tier == tier and restaurant.plan_tier == tier
            and not current.cancel_at_period_end):
        raise AlreadyOnPlan()
    kind = change_kind(current, tier, restaurant.plan_tier)

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
    if sub.status in ENDED:
        return sub
    if sub.status != Subscription.Status.CREATED:
        # Authorised before: a second verify, a replayed
        # subscription.authenticated, or mark_active for a charge or a sync.
        # Everything below happened then; re-running it would cancel what
        # replaced this subscription since (2026-10-12: syncing an old
        # subscription, still "active" on Razorpay until its paid month ends,
        # cancelled the new plan) or reopen an organization nothing paid for.
        return sub
    now = timezone.now()
    sub.status = Subscription.Status.AUTHENTICATED
    old = sub.replaces
    # The plan just chosen replaces every other live one, not only the one
    # it was checked out against (2026-10-08): a downgrade still waiting to
    # start, or a second checkout authorised meanwhile, would otherwise all
    # begin charging at the same date. One that is billing keeps its paid
    # period (cancelled at cycle end); one that hasn't started is cancelled.
    others = sub.restaurant.subscriptions.filter(status__in=LIVE, cancel_at_period_end=False).exclude(id=sub.id)
    for other in others:
        try:
            razorpay_client.cancel_subscription(other.razorpay_subscription_id, at_cycle_end=other.status == Subscription.Status.ACTIVE)
        except RazorpayUnavailableError:
            logger.exception("Could not cancel replaced subscription %s", other.razorpay_subscription_id)
        other.cancel_at_period_end = True
        other.save(update_fields=["cancel_at_period_end", "updated_at"])
    kind = change_kind(old if old is not None and old.plan_applied_at else None, sub.plan_tier, sub.restaurant.plan_tier)
    # An upgrade (or a first plan) applies now; a downgrade waits for billing.
    if kind in ("new", "upgrade"):
        _apply_tier(sub, now)
        _restore_access(sub.restaurant)
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
            # Fields Razorpay sends as null must not 500 the webhook (2026-10-12).
            "amount": Decimal(payment.get("amount") or 0) / 100, "currency": payment.get("currency") or "INR",
            "method": (payment.get("method") or "")[:20], "razorpay_invoice_id": payment.get("invoice_id") or "",
            "razorpay_order_id": payment.get("order_id") or "",
            "plan_tier": sub.plan_tier, "period_start": sub.current_start, "period_end": sub.current_end,
        },
    )
    changed = []
    if not created and record.status != status and status == SubscriptionPayment.Status.PAID:
        record.status = status
        changed.append("status")
    if not record.razorpay_order_id and payment.get("order_id"):
        record.razorpay_order_id = payment["order_id"]  # rows recorded before the field existed
        changed.append("razorpay_order_id")
    if changed:
        record.save(update_fields=changed)
    return record


PLAN_CACHE_SECONDS = 86400


def razorpay_subscription_details(sub):
    """GET /platform/tenants/{id}/subscription/ (2026-10-20, per Karwin):
    the subscription as Razorpay has it right now - its raw status, counts,
    dates and the plan behind it. The plan is cached for a day (Razorpay
    plans can't be edited). Raises RazorpayUnavailableError."""
    from django.core.cache import cache

    entity = razorpay_client.fetch_subscription(sub.razorpay_subscription_id)
    plan_id = entity.get("plan_id") or sub.razorpay_plan_id
    plan = cache.get(f"razorpay-plan:{plan_id}")
    if plan is None:
        plan = razorpay_client.fetch_plan(plan_id)
        cache.set(f"razorpay-plan:{plan_id}", plan, PLAN_CACHE_SECONDS)
    item = plan.get("item") or {}
    status = entity.get("status")

    def count(value):
        return int(value) if value is not None and str(value).lstrip("-").isdigit() else None

    return {
        "razorpay_subscription_id": entity.get("id") or sub.razorpay_subscription_id,
        "status": status,
        "plan_id": plan_id,
        "plan_description": item.get("name") or item.get("description") or "",
        "amount": str((Decimal(item.get("amount") or 0) * (count(entity.get("quantity")) or 1) / 100).quantize(Decimal("0.01"))),
        "currency": item.get("currency") or "INR",
        "interval": plan.get("period"),
        "interval_count": count(plan.get("interval")),
        "payment_method": entity.get("payment_method"),
        "start_at": _aware(entity.get("start_at")),
        "end_at": _aware(entity.get("end_at")),
        "total_count": count(entity.get("total_count")),
        "paid_count": count(entity.get("paid_count")),
        "remaining_count": count(entity.get("remaining_count")),
        # The authorisation link - only useful before the mandate is in place.
        "short_url": entity.get("short_url") if status in ("created", "authenticated") else None,
    }


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


def sync_from_razorpay(sub):
    """Bring one subscription in line with what Razorpay says now: its
    status, the paid period's dates and the paid charges (2026-10-12, per
    Karwin). Called at verify and whenever Razorpay sends a payment event
    for a subscription invoice - so nothing depends on the subscription.*
    webhook events being ticked on the Razorpay dashboard (they weren't, and
    dates, invoices and statuses never arrived). Each step is the same one
    the matching webhook takes, so it is safe to repeat."""
    entity = razorpay_client.fetch_subscription(sub.razorpay_subscription_id)
    status = entity.get("status")
    sub.refresh_from_db()
    if sub.status == Subscription.Status.CREATED and status not in ("authenticated", "active"):
        # A checkout never verified with us (2026-10-15): only a mandate
        # Razorpay confirms counts. An abandoned one changes nothing - above
        # all it must not end the organization's access.
        return sub
    if status == "active":
        sub = mark_active(sub, entity)
    elif status == "authenticated" and sub.status == Subscription.Status.CREATED:
        sub = mark_authorised(sub)
    elif status == "pending" and sub.status != Subscription.Status.PENDING:
        sub = mark_renewal_failed(sub, halted=False)
    elif status == "halted" and sub.status != Subscription.Status.HALTED:
        sub = mark_renewal_failed(sub, halted=True)
    elif status in ("cancelled", "completed"):
        sub = mark_ended(sub, completed=status == "completed")
    invoices = (razorpay_client.fetch_subscription_invoices(sub.razorpay_subscription_id) or {}).get("items", [])
    paid = [i for i in invoices if i.get("status") == "paid" and i.get("payment_id")]
    payments = {}
    if paid and sub.status in LIVE and not entity.get("current_end"):
        sub = _period_from_charge(sub, max(paid, key=lambda i: i.get("paid_at") or 0), payments)
    for invoice in paid:
        payment_id = invoice["payment_id"]
        if not SubscriptionPayment.objects.filter(razorpay_payment_id=payment_id).exists():
            payment = payments.get(payment_id) or razorpay_client.fetch_payment(payment_id)
            record_payment(sub, payment, SubscriptionPayment.Status.PAID)
    return sub


def _plus_one_month(moment):
    """The same day next month, in Indian time like Razorpay's monthly
    cycle, clamped to the last day of a shorter month (31 Jan -> 28 Feb)."""
    import calendar

    local = timezone.localtime(moment)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return local.replace(year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1]))


def _period_from_charge(sub, invoice, payments):
    """A paid charge, but no billing period from Razorpay (2026-10-12, per
    Karwin - Altaza): an e-mandate subscription charged at checkout stays
    "created" on Razorpay with current_start, current_end and charge_at all
    null, even after the bank confirmed the mandate, so renews_at had
    nothing to show. The period is then the one that charge paid for: the
    invoice's own billing dates if Razorpay set them, otherwise from when it
    was paid to the same day next month. Never replaces a later end Razorpay
    already gave; Razorpay's own dates overwrite these once it sends them
    (mark_active)."""
    start = _aware(invoice.get("billing_start")) or _aware(invoice.get("paid_at"))
    if start is None:
        payments[invoice["payment_id"]] = razorpay_client.fetch_payment(invoice["payment_id"])
        start = _aware(payments[invoice["payment_id"]].get("created_at"))
    if start is None:
        return sub
    end = _aware(invoice.get("billing_end")) or _plus_one_month(start)
    with transaction.atomic():
        sub = _lock(sub)
        if sub.current_end is not None and sub.current_end >= end:
            return sub
        sub.current_start, sub.current_end = start, end
        sub.save(update_fields=["current_start", "current_end", "updated_at"])
    SubscriptionPayment.objects.filter(subscription=sub, razorpay_payment_id=invoice["payment_id"], period_start__isnull=True).update(
        period_start=start, period_end=end,
    )
    return sub


SYNC_ON_READ_SECONDS = 600
UNVERIFIED_CHECKOUT_DAYS = 7


def sync_live_on_read(restaurant):
    """Re-read the restaurant's live subscriptions from Razorpay when the app
    opens the subscription or invoices screen - at most once every 10
    minutes each (2026-10-12). Razorpay announces some changes only through
    subscription.* events: an e-mandate subscription stays "created" until
    the bank confirms the mandate, then becomes active with its billing
    period, and only subscription.activated says so. With this the dates and
    status show up as soon as Razorpay has them, whatever is ticked on the
    webhook. Best effort - a Razorpay hiccup never fails the screen."""
    from django.core.cache import cache

    from apps.restaurant.realtime import broadcast_if_changed, organization_payload

    # Also recent checkouts never verified with us (2026-10-15, org "b"): the
    # app's verify call can be lost after Razorpay authorised the mandate, and
    # only the (unticked) subscription.authenticated event would say so.
    recent = timezone.now() - datetime.timedelta(days=UNVERIFIED_CHECKOUT_DAYS)
    live = list(restaurant.subscriptions.filter(
        Q(status__in=LIVE) | Q(status=Subscription.Status.CREATED, created_at__gte=recent)))
    due = [s for s in live if cache.add(f"subscription-sync:{s.id}", 1, SYNC_ON_READ_SECONDS)]
    if not due:
        return
    before = organization_payload(restaurant)
    for sub in due:
        try:
            sync_from_razorpay(sub)
        except RazorpayUnavailableError:
            logger.warning("Could not sync %s from Razorpay on read", sub.razorpay_subscription_id)
    broadcast_if_changed(restaurant, before)


def end_trial_if_over(restaurant):
    """Nothing used to end a trial (2026-10-15, per Karwin, msg 1224): once
    trial_ends_at passed the organization stayed TRIAL for good - free
    access, status "trialing" forever. Called wherever the organization's
    status decides access (every staff and kitchen request, login, the
    customer QR, the Super Admin's organization list), so the trial ends at
    the first request after trial_ends_at; no scheduled job needed.
      - a subscription is taking over (authorised, its first charge due on
        Razorpay's side) -> ACTIVE. Razorpay charges by itself; a failed
        charge then goes the usual past_due -> halted -> PAYMENT_DUE way.
      - nothing taking over -> PAYMENT_DUE: only the Admin can sign in, to
        pick a plan.
    Returns True when it changed the organization."""
    from apps.restaurant.models import Restaurant

    now = timezone.now()
    if restaurant is None or restaurant.status != Restaurant.Status.TRIAL or not restaurant.trial_ends_at or restaurant.trial_ends_at > now:
        return False
    from apps.restaurant.realtime import broadcast_if_changed, organization_payload

    before = organization_payload(restaurant)
    # A checkout whose verify call was lost may still have been authorised
    # on Razorpay - find out before deciding (best effort).
    for sub in restaurant.subscriptions.filter(
        status=Subscription.Status.CREATED, created_at__gte=now - datetime.timedelta(days=UNVERIFIED_CHECKOUT_DAYS)
    ):
        try:
            sync_from_razorpay(sub)
        except RazorpayUnavailableError:
            logger.warning("Could not check %s at trial end", sub.razorpay_subscription_id)
    with transaction.atomic():
        locked = Restaurant.objects.select_for_update().get(id=restaurant.id)
        if locked.status != Restaurant.Status.TRIAL or not locked.trial_ends_at or locked.trial_ends_at > now:
            return False  # changed meanwhile (a verify, a webhook, a Super Admin)
        taking_over = locked.subscriptions.filter(status__in=LIVE).filter(
            Q(cancel_at_period_end=False) | Q(current_end__gt=now)
        ).exists()
        locked.status = Restaurant.Status.ACTIVE if taking_over else Restaurant.Status.PAYMENT_DUE
        locked.is_active = taking_over
        locked.save(update_fields=["status", "is_active"])
        _log(locked, "SUBSCRIPTION_STARTED" if taking_over else "SUBSCRIPTION_ENDED",
             "Trial ended; the subscription takes over" if taking_over else "Trial ended without a subscription; a plan is needed to continue")
    restaurant.status, restaurant.is_active = locked.status, locked.is_active
    broadcast_if_changed(restaurant, before)
    return True


def end_expired_trials():
    """Every organization whose trial is over - for the Super Admin's lists,
    where no request from the organization itself may have come in yet."""
    from apps.restaurant.models import Restaurant

    for restaurant in Restaurant.objects.filter(status=Restaurant.Status.TRIAL, trial_ends_at__lte=timezone.now()):
        end_trial_if_over(restaurant)


def sync_for_invoice_payment(payment):
    """A payment.captured / payment.failed for a subscription invoice: find
    our subscription through the invoice and sync it. Returns what happened,
    for the webhook's reply."""
    invoice = razorpay_client.fetch_invoice(payment["invoice_id"])
    sub = Subscription.objects.filter(razorpay_subscription_id=invoice.get("subscription_id")).select_related("restaurant").first()
    if sub is None or sub.status in ENDED:
        return "ignored"
    from apps.restaurant.realtime import broadcast_if_changed, organization_payload

    restaurant = sub.restaurant
    before = organization_payload(restaurant)
    sub = sync_from_razorpay(sub)
    if payment.get("status") == "failed" and payment.get("id"):
        record_payment(sub, payment, SubscriptionPayment.Status.FAILED)
    broadcast_if_changed(restaurant, before)
    return "synced"


def handle_webhook(event, payload):
    """Razorpay subscription.* events. Unknown subscriptions are ignored
    (another environment's, or one created outside DineOS)."""
    entity = ((payload.get("payload") or {}).get("subscription") or {}).get("entity") or {}
    sub = Subscription.objects.filter(razorpay_subscription_id=entity.get("id")).select_related("restaurant").first()
    if sub is None or sub.status in ENDED:
        return "ignored"
    from apps.restaurant.realtime import broadcast_if_changed, organization_payload

    restaurant = sub.restaurant
    before = organization_payload(restaurant)
    result = _apply_event(event, sub, entity, payload)
    broadcast_if_changed(restaurant, before)
    return result


def _apply_event(event, sub, entity, payload):
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
