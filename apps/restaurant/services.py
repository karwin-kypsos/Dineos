import base64
import io

import qrcode
from django.conf import settings
from django.db import transaction


def build_table_qr(restaurant_slug, branch_slug, table_number):
    """Returns (qr_url, qr_code_data_uri) for a table's printed QR code —
    qr_url is what the code encodes (opens the customer ordering app),
    qr_code_data_uri is a ready-to-render base64 PNG for <img src=...>."""
    # Path is {org_slug}/{branch_slug}/table/{table_number} (2026-08-25, per
    # Shereena, once the Customer Web App was deployed) — the "table/"
    # segment before the number, not just the bare number.
    qr_url = f"{settings.CUSTOMER_APP_BASE_URL}/{restaurant_slug}/{branch_slug}/table/{table_number}"

    img = qrcode.make(qr_url)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    return qr_url, f"data:image/png;base64,{encoded}"


def sync_branch_tables(branch, new_count):
    """Keep Table rows in step with Branch.table_count.

    Only ever ADDS tables when the count increases, and only ever
    DEACTIVATES (never deletes) tables when it decreases. Existing table
    numbers are never touched or renumbered — a restaurant's already
    printed, physically-stuck-to-the-table QR codes must keep working.
    """
    from apps.tables.models import Table

    if new_count is None:
        return

    existing_numbers = Table.objects.filter(branch=branch).values_list("table_number", flat=True)
    existing_max = 0
    for n in existing_numbers:
        try:
            existing_max = max(existing_max, int(n))
        except ValueError:
            continue

    if new_count > existing_max:
        with transaction.atomic():
            for i in range(existing_max + 1, new_count + 1):
                Table.objects.get_or_create(
                    branch=branch,
                    table_number=str(i),
                    defaults={"restaurant": branch.restaurant, "capacity": 4},
                )
    elif new_count < existing_max:
        with transaction.atomic():
            Table.objects.filter(
                branch=branch,
                table_number__in=[str(i) for i in range(new_count + 1, existing_max + 1)],
            ).update(is_active=False)
