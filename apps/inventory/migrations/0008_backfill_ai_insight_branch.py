"""Give branch-less AI insights their ingredient's branch.

2026-09-29. generate_ai_insights tagged each insight with the caller's
branch instead of the ingredient's. An Admin has no branch, so every
insight an Admin generated was saved branch-less, and since 2026-09-21 a
branch's dashboard and its Manager's insight feed only show insights for
that exact branch - those insights never reached the people they were
about. The insight is always about one ingredient, and that ingredient's
branch is the right answer, so this copies it across.

Insights whose ingredient is itself branch-less (it predates Branch) stay
branch-less: there is no branch to give them.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    AIInsight = apps.get_model("inventory", "AIInsight")
    for insight in AIInsight.objects.filter(
        branch__isnull=True, ingredient__branch__isnull=False
    ).select_related("ingredient"):
        insight.branch_id = insight.ingredient.branch_id
        insight.save(update_fields=["branch"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0007_backfill_purchase_order_statuses"),
    ]

    operations = [
        # Nothing to reverse: the null branch was the bug, not information.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
