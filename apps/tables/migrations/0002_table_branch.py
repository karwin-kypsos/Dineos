import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tables', '0001_initial'),
        ('restaurant', '0002_branch'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='table',
            name='one_table_number_per_restaurant',
        ),
        migrations.AddField(
            model_name='table',
            name='branch',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='tables', to='restaurant.branch',
            ),
        ),
        migrations.AddConstraint(
            model_name='table',
            constraint=models.UniqueConstraint(
                condition=models.Q(('branch__isnull', True)),
                fields=('restaurant', 'table_number'),
                name='one_table_number_per_restaurant_legacy',
            ),
        ),
        migrations.AddConstraint(
            model_name='table',
            constraint=models.UniqueConstraint(
                condition=models.Q(('branch__isnull', False)),
                fields=('branch', 'table_number'),
                name='one_table_number_per_branch',
            ),
        ),
    ]
