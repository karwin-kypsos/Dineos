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
            "billing_enabled": True,
            "realtime_enabled": False,
        },
    },
    "GROWTH": {
        "max_branches": 5,
        "flags": {
            "notifications_enabled": True,
            "kitchen_enabled": True,
            "billing_enabled": True,
            "realtime_enabled": True,
        },
    },
    "ENTERPRISE": {
        "max_branches": None,
        "flags": {
            "notifications_enabled": True,
            "kitchen_enabled": True,
            "billing_enabled": True,
            "realtime_enabled": True,
        },
    },
}
