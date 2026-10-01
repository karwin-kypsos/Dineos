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
    # realtime_enabled was here until 2026-10-01: every restaurant gets live
    # updates now (per Karwin), so it is no longer a switch or a plan feature.
    # 2026-09-30, per Shereena.
    {
        "key": "customer_ordering_enabled",
        "label": "Customer Ordering",
        "description": "Customers order for themselves from the table QR code. When off, the QR shows the menu only and staff take the orders.",
    },
    {
        "key": "server_staff_enabled",
        "label": "Server Staff",
        "description": "The restaurant can add Server (order-taking) staff accounts. Off for fully self-order restaurants.",
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
