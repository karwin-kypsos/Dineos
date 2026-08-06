from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurant', '0002_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='restaurant',
            name='status',
            field=models.CharField(choices=[('ACTIVE', 'Active'), ('TRIAL', 'Trial'), ('SUSPENDED', 'Suspended')], default='ACTIVE', max_length=16),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='contact_name',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='contact_email',
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='contact_phone',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='billing_email',
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='primary_color',
            field=models.CharField(blank=True, default='#FF6B35', max_length=7),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='plan_tier',
            field=models.CharField(choices=[('STARTER', 'Starter'), ('GROWTH', 'Growth'), ('ENTERPRISE', 'Enterprise')], default='STARTER', max_length=16),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='max_branches',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='default_manager_spending_limit',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
    ]
