import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0001_initial'),
        ('restaurant', '0002_branch'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='category',
            name='one_category_name_per_restaurant',
        ),
        migrations.AddField(
            model_name='category',
            name='branch',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='menu_categories', to='restaurant.branch',
            ),
        ),
        migrations.AddField(
            model_name='menuitem',
            name='is_veg',
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name='category',
            constraint=models.UniqueConstraint(
                condition=models.Q(('branch__isnull', True)),
                fields=('restaurant', 'name'),
                name='one_category_name_per_restaurant_legacy',
            ),
        ),
        migrations.AddConstraint(
            model_name='category',
            constraint=models.UniqueConstraint(
                condition=models.Q(('branch__isnull', False)),
                fields=('branch', 'name'),
                name='one_category_name_per_branch',
            ),
        ),
    ]
