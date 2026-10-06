from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0011_alter_restaurant_status'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='restaurant',
            name='notifications_enabled',
        ),
    ]
