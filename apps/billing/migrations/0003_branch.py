import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0002_cashiershift_cashiershift_one_open_shift_per_cashier'),
        ('restaurant', '0002_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='cashiershift',
            name='branch',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='cashier_shifts', to='restaurant.branch',
            ),
        ),
        migrations.AddField(
            model_name='bill',
            name='branch',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='bills', to='restaurant.branch',
            ),
        ),
    ]
