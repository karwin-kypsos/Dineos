from django.db import transaction


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
