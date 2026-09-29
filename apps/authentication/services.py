from .models import PasswordResetToken

INVITE_TTL_MINUTES = 60 * 24 * 7  # 7 days — invites live longer than a forgot-password reset


def issue_invite(user):
    """Locks the account (no usable password) and issues a long-lived
    PasswordResetToken for it. The user can't log in via /v1/auth/login/
    until they complete the invite by POSTing the token + a chosen password
    to /v1/auth/reset-password/, which also logs them straight in — the
    same mechanism as a forgot-password reset, just a longer expiry and a
    must_change_password flag cleared on completion instead of set.
    """
    user.set_unusable_password()
    user.must_change_password = True
    user.save(update_fields=["password", "must_change_password"])
    return PasswordResetToken.issue(user, ttl_minutes=INVITE_TTL_MINUTES, kind="invite")


# ---- Admin Self-Registration (2026-09-29) --------------------------------------

REGISTRATION_TTL_HOURS = 24
# Same trial a Super Admin gives a tenant created straight into TRIAL, so a
# self-registered restaurant shows up in the platform dashboard's "needing
# attention" list when it runs out and can be switched to ACTIVE there.
SELF_REGISTRATION_TRIAL_DAYS = 14


class RegistrationNotFound(Exception):
    pass


class RegistrationExpired(Exception):
    pass


class RegistrationAlreadyCompleted(Exception):
    pass


class EmailAlreadyRegistered(Exception):
    pass


def start_self_registration(data):
    """Step 2: keep the details and chosen plan until the Admin sets a login.
    Nothing is created on the platform yet - see RestaurantRegistration."""
    from django.utils import timezone

    from apps.restaurant.models import RestaurantRegistration

    return RestaurantRegistration.objects.create(
        restaurant_name=data["restaurant_name"],
        contact_name=data["contact_name"],
        contact_phone=data["contact_phone"],
        contact_email=data["contact_email"],
        billing_email=data["billing_email"] or data["contact_email"],
        gst_number=data["gst_number"],
        service_charge_percentage=data["service_charge_percentage"],
        plan_tier=data["plan_id"],
        expires_at=timezone.now() + timezone.timedelta(hours=REGISTRATION_TTL_HOURS),
    )


def _unique_restaurant_slug(name):
    from django.utils.text import slugify

    from apps.restaurant.models import Restaurant

    base = slugify(name)[:56].strip("-") or "restaurant"
    slug, n = base, 2
    while Restaurant.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def complete_self_registration(registration_id, email, password):
    """Step 3: create the restaurant from its registration, with the plan's
    branch limit and modules, and its first Admin - the same shape a Super
    Admin's Create Organization produces, minus the invite, since the Admin
    is choosing their own password right here.

    Locked on the registration row, so a double-tapped signup creates one
    restaurant, and atomic, so a failure never leaves a restaurant with no
    Admin behind.
    """
    from decimal import Decimal

    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone

    from apps.platform.models import PlatformActivityLog
    from apps.restaurant.models import Restaurant, RestaurantRegistration
    from apps.restaurant.plans import PLAN_PRESETS

    User = get_user_model()

    with transaction.atomic():
        registration = RestaurantRegistration.objects.select_for_update().filter(id=registration_id).first()
        if registration is None:
            raise RegistrationNotFound()
        if registration.status == RestaurantRegistration.Status.COMPLETED:
            raise RegistrationAlreadyCompleted()
        now = timezone.now()
        if registration.expires_at <= now:
            raise RegistrationExpired()
        if User.objects.filter(email__iexact=email).exists():
            raise EmailAlreadyRegistered()

        preset = PLAN_PRESETS[registration.plan_tier]
        restaurant = Restaurant.objects.create(
            name=registration.restaurant_name,
            slug=_unique_restaurant_slug(registration.restaurant_name),
            status=Restaurant.Status.TRIAL,
            trial_ends_at=now + timezone.timedelta(days=SELF_REGISTRATION_TRIAL_DAYS),
            gst_percentage=Decimal(str(settings.DEFAULT_GST_PERCENTAGE)),
            service_charge_percentage=registration.service_charge_percentage,
            contact_name=registration.contact_name,
            contact_email=registration.contact_email,
            contact_phone=registration.contact_phone,
            billing_email=registration.billing_email,
            gst_number=registration.gst_number,
            plan_tier=registration.plan_tier,
            max_branches=preset["max_branches"],
            **preset["flags"],
        )
        admin = User.objects.create_user(
            email=email, password=password, name=registration.contact_name,
            phone=registration.contact_phone, role=User.Role.ADMIN, restaurant=restaurant,
        )

        registration.status = RestaurantRegistration.Status.COMPLETED
        registration.restaurant = restaurant
        registration.completed_at = now
        registration.save(update_fields=["status", "restaurant", "completed_at"])

        # No platform actor - the restaurant signed itself up - but it
        # still belongs in the Super Admin's activity log.
        PlatformActivityLog.objects.create(
            actor=None, action="TENANT_CREATED", restaurant=restaurant,
            description=(
                f"'{restaurant.name}' ({restaurant.slug}) signed itself up "
                f"on the {restaurant.get_plan_tier_display()} plan"
            )[:255],
        )
    return restaurant, admin
