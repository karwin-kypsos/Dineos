import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_branch'),
        ('orders', '0003_takeaway'),
    ]

    operations = [
        migrations.AlterField(
            model_name='bill',
            name='session',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='bill', to='tables.tablesession'),
        ),
        migrations.AddField(
            model_name='bill',
            name='order',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='takeaway_bill', to='orders.order'),
        ),
        migrations.AddConstraint(
            model_name='bill',
            constraint=models.CheckConstraint(
                check=models.Q(('session__isnull', False), ('order__isnull', True)) | models.Q(('session__isnull', True), ('order__isnull', False)),
                name='bill_has_exactly_one_of_session_or_order',
            ),
        ),
    ]
