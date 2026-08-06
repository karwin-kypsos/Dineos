import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('kitchen', '0001_initial'),
        ('restaurant', '0002_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='kdsdevice',
            name='branch',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='kds_devices', to='restaurant.branch',
            ),
        ),
    ]
