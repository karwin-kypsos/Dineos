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
            # On since 2026-09-30, per Karwin: live updates in every plan.
            "realtime_enabled": True,
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
            "realtime_enabled": True,
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
            "realtime_enabled": True,
            "customer_ordering_enabled": True,
            "server_staff_enabled": True,
        },
    },
}

# What each plan costs, for the public Get Plans list (2026-09-29, Admin
# Self-Registration). Hardcoded next to the presets on purpose, like the
# presets themselves - the build guide says no database plan editor.
# price is a decimal string in rupees and billing_cycle is "MONTHLY" or
# "YEARLY"; both stay None until Karwin sets the real figures, and the
# API returns null rather than a made-up number.
PLAN_PRICING = {
    "STARTER": {"price": None, "billing_cycle": None},
    "GROWTH": {"price": None, "billing_cycle": None},
    "ENTERPRISE": {"price": None, "billing_cycle": None},
}
