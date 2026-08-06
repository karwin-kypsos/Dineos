from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.kitchen.models import KDSDevice
from apps.menu.models import Category, MenuItem, PreparedPortion
from apps.platform.models import PlatformAdmin
from apps.restaurant.models import Restaurant
from apps.tables.models import Table

User = get_user_model()


class Command(BaseCommand):
    help = "Seed demo data for local development / smoke testing — two tenants, for tenant-isolation checks."

    def handle(self, *args, **options):
        if not PlatformAdmin.objects.filter(email="super@dineos.platform").exists():
            PlatformAdmin.objects.create_user(email="super@dineos.platform", password="Demo@1234", name="Super Admin")
            self.stdout.write("Created platform admin super@dineos.platform / Demo@1234")

        self._seed_restaurant("demo-bistro", "DineOS Demo Bistro", "Chicken Biryani", "Main Kitchen Tablet")
        self._seed_restaurant("spice-garden", "Spice Garden", "Paneer Tikka", "Spice Garden Kitchen Tablet")

    def _seed_restaurant(self, slug, name, dish_name, kds_label):
        restaurant, _ = Restaurant.objects.get_or_create(slug=slug, defaults={"name": name})
        self.stdout.write(f"Restaurant: {restaurant.name} ({restaurant.slug})")

        demo_users = [
            (f"admin@{slug}.demo", "ADMIN", "Admin"),
            (f"manager@{slug}.demo", "MANAGER", "Manager"),
            (f"server@{slug}.demo", "SERVER", "Server"),
            (f"cashier@{slug}.demo", "CASHIER", "Cashier"),
        ]
        for email, role, user_name in demo_users:
            if not User.objects.filter(email=email).exists():
                User.objects.create_user(email=email, password="Demo@1234", role=role, name=user_name, restaurant=restaurant)
                self.stdout.write(f"  Created user {email} / Demo@1234")

        # branch=None here on purpose: this is the legacy, ungrouped table set.
        # A branch can have its own same-numbered tables (see restaurant.services.
        # sync_branch_tables), so the lookup must stay branch-scoped or it can
        # match more than one row once any branch has been created.
        table, _ = Table.objects.get_or_create(
            restaurant=restaurant, table_number="5", branch=None, defaults={"capacity": 4}
        )
        Table.objects.get_or_create(restaurant=restaurant, table_number="6", branch=None, defaults={"capacity": 2})
        self.stdout.write(f"  Table: {table.table_number} (id={table.id})")

        category, _ = Category.objects.get_or_create(
            restaurant=restaurant, name="Main Course", branch=None, defaults={"emoji": "🍽️", "sort_order": 1}
        )
        menu_item, _ = MenuItem.objects.get_or_create(
            category=category,
            name=dish_name,
            defaults={"price": Decimal("220.00"), "description": f"{dish_name} — chef's special"},
        )
        self.stdout.write(f"  Menu item: {menu_item.name} (id={menu_item.id})")

        PreparedPortion.objects.get_or_create(
            menu_item=menu_item,
            date=timezone.localdate(),
            defaults={"portions_initial": 20, "portions_remaining": 20},
        )
        self.stdout.write(f"  Prepared 20 portions of {menu_item.name} for today")

        device, _ = KDSDevice.objects.get_or_create(restaurant=restaurant, label=kds_label)
        self.stdout.write(f"  KDS device API key: {device.api_key}")
