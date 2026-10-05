"""Plan presets — an internal starting template only, not a permanent
lock. Picking a plan on Create Organization pre-fills max_branches and the
per-tenant add-on flags; every value stays individually overridable
afterward on the Restaurant row itself (see PlatformAdmin's "Feature
Flags" toggles on Organization Detail)."""

PLAN_PRESETS = {
    "STARTER": {
        "max_branches": 1,
        "flags": {
            "notifications_enabled": True,
            "kitchen_enabled": False,
            # Off since 2026-09-29, per Karwin: billing is a Growth/Enterprise
            # module. A preset only fills new organizations and plan changes -
            # restaurants already on Starter keep whatever their row says.
            "billing_enabled": False,
            # No realtime_enabled: every plan has live updates and it stopped
            # being a flag on 2026-10-01, per Karwin.
            # Off since 2026-09-30, per Shereena - same caveat as billing.
            "customer_ordering_enabled": False,
            "server_staff_enabled": False,
        },
    },
    "GROWTH": {
        "max_branches": 5,
        "flags": {
            "notifications_enabled": True,
            "kitchen_enabled": True,
            "billing_enabled": True,
            "customer_ordering_enabled": True,
            "server_staff_enabled": True,
        },
    },
    "ENTERPRISE": {
        "max_branches": None,
        "flags": {
            "notifications_enabled": True,
            "kitchen_enabled": True,
            "billing_enabled": True,
            "customer_ordering_enabled": True,
            "server_staff_enabled": True,
        },
    },
}

# Prices live in settings.PLAN_PRICES (PLAN_PRICE_<TIER> env vars, rupees a
# month) since 2026-10-05 - see apps.subscriptions.services.plan_price.


def apply_plan_preset(restaurant, plan_tier):
    """Put a restaurant on a plan: tier, branch limit and add-on flags from
    its preset (what Super Admin's plan change does), saved."""
    preset = PLAN_PRESETS[plan_tier]
    restaurant.plan_tier = plan_tier
    restaurant.max_branches = preset["max_branches"]
    for flag, value in preset["flags"].items():
        setattr(restaurant, flag, value)
    restaurant.save(update_fields=["plan_tier", "max_branches", *preset["flags"].keys()])
    return restaurant
