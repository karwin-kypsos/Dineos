from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.kitchen.models import KDSDevice
from apps.menu.models import Category, MenuItem, PreparedPortion
from apps.restaurant.models import Restaurant
from apps.tables.models import Table

User = get_user_model()


class Command(BaseCommand):
    help = "Seed demo data for local development / smoke testing."

    def handle(self, *args, **options):
        restaurant = Restaurant.load()
        restaurant.name = "DineOS Demo Bistro"
        restaurant.save()
        self.stdout.write(f"Restaurant: {restaurant.name}")

        demo_users = [
            ("admin@dineos.demo", "ADMIN", "Admin"),
            ("manager@dineos.demo", "MANAGER", "Manager"),
            ("server@dineos.demo", "SERVER", "Server"),
            ("cashier@dineos.demo", "CASHIER", "Cashier"),
        ]
        for email, role, name in demo_users:
            if not User.objects.filter(email=email).exists():
                User.objects.create_user(email=email, password="Demo@1234", role=role, name=name)
                self.stdout.write(f"Created user {email} / Demo@1234")

        table, _ = Table.objects.get_or_create(table_number="5", defaults={"capacity": 4})
        Table.objects.get_or_create(table_number="6", defaults={"capacity": 2})
        self.stdout.write(f"Table: {table.table_number} (id={table.id})")

        category, _ = Category.objects.get_or_create(name="Main Course", defaults={"emoji": "🍽️", "sort_order": 1})
        menu_item, _ = MenuItem.objects.get_or_create(
            name="Chicken Biryani",
            defaults={"category": category, "price": Decimal("220.00"), "description": "Spiced rice with chicken"},
        )
        self.stdout.write(f"Menu item: {menu_item.name} (id={menu_item.id})")

        PreparedPortion.objects.get_or_create(
            menu_item=menu_item,
            date=timezone.localdate(),
            defaults={"portions_initial": 20, "portions_remaining": 20},
        )
        self.stdout.write("Prepared 20 portions of Chicken Biryani for today")

        device, created = KDSDevice.objects.get_or_create(label="Main Kitchen Tablet")
        self.stdout.write(f"KDS device API key: {device.api_key}")
