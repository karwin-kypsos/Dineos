import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0001_initial'),
        ('restaurant', '0002_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='branch',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='orders', to='restaurant.branch',
            ),
        ),
    ]
