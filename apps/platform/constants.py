"""Static, platform-wide metadata for the Super Admin app's Organization
Detail / Create Organization screens — not per-tenant data, just the
descriptive labels the frontend renders next to each toggle/swatch.
"""

FEATURE_FLAG_METADATA = [
    {
        "key": "kitchen_enabled",
        "label": "Kitchen Display",
        "description": "Kitchen Display Screen for accepting/preparing/readying orders.",
    },
    {
        "key": "billing_enabled",
        "label": "Billing",
        "description": "Bill preview, payment collection, and cashier shift reconciliation.",
    },
    {
        "key": "notifications_enabled",
        "label": "Notifications",
        "description": "In-app notifications for order-ready, bill-requested, and payment-confirmed events.",
    },
    {
        "key": "realtime_enabled",
        "label": "Realtime Updates",
        "description": "Live WebSocket updates for kitchen, tables, and notifications instead of manual refresh.",
    },
]

# A curated set of brand-safe hex colors for the primary-color picker —
# organizations aren't limited to these (primary_color accepts any hex),
# this is just the quick-pick preset list the swatch UI offers first.
THEME_COLOR_PRESETS = [
    {"label": "DineOS Orange", "hex": "#FF6B35"},
    {"label": "Indigo", "hex": "#4F46E5"},
    {"label": "Emerald", "hex": "#16A34A"},
    {"label": "Amber", "hex": "#D97706"},
    {"label": "Rose", "hex": "#E11D48"},
    {"label": "Sky", "hex": "#0284C7"},
    {"label": "Violet", "hex": "#7C3AED"},
    {"label": "Slate", "hex": "#334155"},
]
