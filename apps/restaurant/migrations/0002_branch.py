import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0001_initial'),
        ('authentication', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Branch',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('slug', models.SlugField(blank=True, max_length=64)),
                ('address', models.CharField(blank=True, max_length=500)),
                ('phone', models.CharField(blank=True, max_length=32)),
                ('photo_url', models.URLField(blank=True)),
                ('table_count', models.PositiveIntegerField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('restaurant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='branches', to='restaurant.restaurant')),
                ('manager', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='managed_branches', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'branches',
                'ordering': ['name'],
            },
        ),
        migrations.AddConstraint(
            model_name='branch',
            constraint=models.UniqueConstraint(fields=('restaurant', 'name'), name='one_branch_name_per_restaurant'),
        ),
        migrations.AddConstraint(
            model_name='branch',
            constraint=models.UniqueConstraint(fields=('restaurant', 'slug'), name='one_branch_slug_per_restaurant'),
        ),
    ]
