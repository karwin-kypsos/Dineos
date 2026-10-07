from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def backfill_quoted_cost(apps, schema_editor):
    """Receipts recorded before 2026-10-07 added their stock at the PO line's
    quoted unit_cost - record that as what the delivery cost."""
    GoodsReceiptLine = apps.get_model("inventory", "GoodsReceiptLine")
    PurchaseOrderLine = apps.get_model("inventory", "PurchaseOrderLine")
    GoodsReceiptLine.objects.filter(unit_cost__isnull=True).update(
        unit_cost=Subquery(PurchaseOrderLine.objects.filter(id=OuterRef("purchase_order_line_id")).values("unit_cost")[:1])
    )


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0009_case_insensitive_name_ordering'),
    ]

    operations = [
        migrations.AddField(
            model_name='goodsreceiptline',
            name='unit_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.RunPython(backfill_quoted_cost, migrations.RunPython.noop),
    ]
