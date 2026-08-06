import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0002_order_branch'),
    ]

    operations = [
        migrations.AlterField(
            model_name='order',
            name='session',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='orders', to='tables.tablesession'),
        ),
        migrations.AlterField(
            model_name='order',
            name='table',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='orders', to='tables.table'),
        ),
        migrations.AlterField(
            model_name='order',
            name='round_number',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='order',
            name='order_type',
            field=models.CharField(choices=[('DINE_IN', 'Dine-in'), ('TAKEAWAY', 'Takeaway')], default='DINE_IN', max_length=16),
        ),
        migrations.AddField(
            model_name='order',
            name='customer_name',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='order',
            name='customer_phone',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.CheckConstraint(
                check=models.Q(('order_type', 'DINE_IN'), ('session__isnull', False), ('table__isnull', False)) | models.Q(('order_type', 'TAKEAWAY'), ('session__isnull', True), ('table__isnull', True)),
                name='dine_in_has_table_takeaway_does_not',
            ),
        ),
    ]
