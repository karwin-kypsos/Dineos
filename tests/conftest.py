from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.kitchen.models import KDSDevice
from apps.menu.models import Category, MenuItem, PreparedPortion
from apps.tables.models import Table

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


def _staff_client(email, role):
    user = User.objects.create_user(email=email, password="Test@1234", role=role, name=role.title())
    client = APIClient()
    token = RefreshToken.for_user(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return user, client


@pytest.fixture
def admin_client():
    return _staff_client("admin@test.dineos", "ADMIN")


@pytest.fixture
def manager_client():
    return _staff_client("manager@test.dineos", "MANAGER")


@pytest.fixture
def server_client():
    return _staff_client("server@test.dineos", "SERVER")


@pytest.fixture
def cashier_client():
    return _staff_client("cashier@test.dineos", "CASHIER")


@pytest.fixture
def kds_device():
    return KDSDevice.objects.create(label="Test KDS")


@pytest.fixture
def kds_client(kds_device):
    client = APIClient()
    client.credentials(HTTP_X_KDS_API_KEY=kds_device.api_key)
    return kds_device, client


@pytest.fixture
def table():
    return Table.objects.create(table_number="5", capacity=4)


@pytest.fixture
def menu_item():
    category = Category.objects.create(name="Main Course", sort_order=1)
    item = MenuItem.objects.create(category=category, name="Chicken Biryani", price=Decimal("220.00"))
    PreparedPortion.objects.create(menu_item=item, date=timezone.localdate(), portions_initial=20, portions_remaining=20)
    return item
