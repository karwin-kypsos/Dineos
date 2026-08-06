import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('restaurant', '0002_branch'),
        ('menu', '0002_branch_and_veg'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Ingredient',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('unit', models.CharField(choices=[('KG', 'Kilograms'), ('G', 'Grams'), ('L', 'Liters'), ('ML', 'Milliliters'), ('PCS', 'Pieces')], max_length=4)),
                ('current_stock', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('unit_cost', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('minimum_stock_level', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('supplier_name', models.CharField(blank=True, max_length=255)),
                ('supplier_notes', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('restaurant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ingredients', to='restaurant.restaurant')),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ingredients', to='restaurant.branch')),
            ],
            options={
                'db_table': 'ingredients',
                'ordering': ['name'],
            },
        ),
        migrations.AddConstraint(
            model_name='ingredient',
            constraint=models.UniqueConstraint(condition=models.Q(('branch__isnull', True)), fields=('restaurant', 'name'), name='one_ingredient_name_per_restaurant_legacy'),
        ),
        migrations.AddConstraint(
            model_name='ingredient',
            constraint=models.UniqueConstraint(condition=models.Q(('branch__isnull', False)), fields=('branch', 'name'), name='one_ingredient_name_per_branch'),
        ),
        migrations.CreateModel(
            name='StockMovement',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('movement_type', models.CharField(choices=[('RESTOCK', 'Restock'), ('WASTAGE', 'Wastage'), ('USAGE', 'Usage'), ('ADJUSTMENT', 'Adjustment')], max_length=16)),
                ('quantity', models.DecimalField(decimal_places=2, max_digits=10)),
                ('wastage_reason', models.CharField(blank=True, choices=[('SPOILED', 'Spoiled'), ('OVER_PREPPED', 'Over-prepped'), ('RETURNED', 'Returned'), ('OTHER', 'Other')], max_length=16)),
                ('reason', models.CharField(blank=True, max_length=255)),
                ('unit_cost_at_time', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('recorded_at', models.DateTimeField(auto_now_add=True)),
                ('ingredient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='movements', to='inventory.ingredient')),
                ('recorded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='stock_movements', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'stock_movements',
                'ordering': ['-recorded_at'],
            },
        ),
        migrations.CreateModel(
            name='PurchaseOrder',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected'), ('ORDERED', 'Ordered'), ('RECEIVED', 'Received')], default='PENDING', max_length=16)),
                ('supplier_name', models.CharField(blank=True, max_length=255)),
                ('supplier_notes', models.TextField(blank=True)),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('restaurant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='purchase_orders', to='restaurant.restaurant')),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='purchase_orders', to='restaurant.branch')),
                ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='requested_purchase_orders', to=settings.AUTH_USER_MODEL)),
                ('approved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='approved_purchase_orders', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'purchase_orders',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='PurchaseOrderLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity_ordered', models.DecimalField(decimal_places=2, max_digits=10)),
                ('quantity_received', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('unit_cost', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('purchase_order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='inventory.purchaseorder')),
                ('ingredient', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='purchase_order_lines', to='inventory.ingredient')),
            ],
            options={
                'db_table': 'purchase_order_lines',
            },
        ),
        migrations.CreateModel(
            name='RecipeItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity_per_serving', models.DecimalField(decimal_places=3, max_digits=10)),
                ('menu_item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recipe_items', to='menu.menuitem')),
                ('ingredient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recipe_items', to='inventory.ingredient')),
            ],
            options={
                'db_table': 'recipe_items',
            },
        ),
        migrations.AddConstraint(
            model_name='recipeitem',
            constraint=models.UniqueConstraint(fields=('menu_item', 'ingredient'), name='one_recipe_line_per_item_ingredient'),
        ),
    ]
