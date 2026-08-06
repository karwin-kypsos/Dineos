import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0001_initial'),
        ('restaurant', '0002_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='notification',
            name='branch',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='notifications', to='restaurant.branch',
            ),
        ),
    ]
