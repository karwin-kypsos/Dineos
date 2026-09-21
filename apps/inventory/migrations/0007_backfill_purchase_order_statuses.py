"""Bring purchase orders already in flight onto the goods-receipt model.

2026-09-21. The status enum changed underneath live rows, so this maps
the old values onto the new ones and fills in the quantities the new
flow expects. Without it, every existing PO would sit on a status the
code no longer recognises and show a blank approved quantity.

Mapping, and why:
  PENDING  -> PENDING_APPROVAL   same thing, renamed.
  ORDERED  -> APPROVED           ORDERED is gone. A PO that was placed
                                 with the supplier but not yet delivered
                                 is, under the new flow, simply approved
                                 and awaiting its first goods receipt.
  RECEIVED -> FULLY_RECEIVED     same thing, renamed.
  APPROVED, REJECTED             unchanged.

Quantities: anything that got past approval has approved_quantity set to
what was requested, because the old flow had no way to approve a
different number. Anything already received has quantity_received filled
to match, since the old receive was all-or-nothing.

Deliberately NOT done: no synthetic GoodsReceipt rows are invented for
historically received POs. Those deliveries really happened, but nobody
recorded who accepted them or when, and fabricating a receipt would put
made-up provenance into an audit trail whose whole purpose is to be
trustworthy. Their stock movements are already on record from the old
path; they simply have no receipt document, which is the truth.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    PurchaseOrder = apps.get_model("inventory", "PurchaseOrder")
    PurchaseOrderLine = apps.get_model("inventory", "PurchaseOrderLine")

    PurchaseOrder.objects.filter(status="PENDING").update(status="PENDING_APPROVAL")
    PurchaseOrder.objects.filter(status="ORDERED").update(status="APPROVED")
    PurchaseOrder.objects.filter(status="RECEIVED").update(status="FULLY_RECEIVED")

    past_approval = PurchaseOrder.objects.filter(
        status__in=["APPROVED", "PARTIALLY_RECEIVED", "FULLY_RECEIVED", "CLOSED"]
    ).values_list("id", flat=True)
    for line in PurchaseOrderLine.objects.filter(
        purchase_order_id__in=list(past_approval), approved_quantity__isnull=True
    ):
        line.approved_quantity = line.quantity_ordered
        line.save(update_fields=["approved_quantity"])

    received = PurchaseOrder.objects.filter(status="FULLY_RECEIVED").values_list("id", flat=True)
    for line in PurchaseOrderLine.objects.filter(purchase_order_id__in=list(received)):
        if line.quantity_received < line.quantity_ordered:
            line.quantity_received = line.quantity_ordered
            line.save(update_fields=["quantity_received"])


def backwards(apps, schema_editor):
    """Put the old status values back. approved_quantity is left as it is
    - the old schema has no such column, so the column goes with the
    reversed schema migration anyway."""
    PurchaseOrder = apps.get_model("inventory", "PurchaseOrder")
    PurchaseOrder.objects.filter(status="PENDING_APPROVAL").update(status="PENDING")
    PurchaseOrder.objects.filter(status="FULLY_RECEIVED").update(status="RECEIVED")
    # PARTIALLY_RECEIVED and CLOSED have no pre-2026-09-21 equivalent;
    # the closest honest answer is APPROVED, i.e. approved and not fully
    # delivered, which is exactly what both of them mean.
    PurchaseOrder.objects.filter(status__in=["PARTIALLY_RECEIVED", "CLOSED"]).update(status="APPROVED")
    PurchaseOrder.objects.filter(status="DRAFT").update(status="PENDING")


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0006_goodsreceipt_purchaseorder_approval_note_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
