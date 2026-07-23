from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.serializers import DineOSTokenObtainPairSerializer
from apps.kitchen.models import KDSDevice
from apps.menu.models import Category, MenuItem, PreparedPortion
from apps.restaurant.models import Restaurant
from apps.tables.models import Table

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def restaurant():
    return Restaurant.objects.create(name="Test Restaurant", slug="test-restaurant")


def _staff_client(email, role, restaurant):
    user = User.objects.create_user(email=email, password="Test@1234", role=role, name=role.title(), restaurant=restaurant)
    client = APIClient()
    # Must go through the custom serializer, not a bare RefreshToken.for_user()
    # — that's the only path that embeds the restaurant_id claim the
    # TenantResolverMiddleware relies on to set request.tenant.
    token = DineOSTokenObtainPairSerializer.get_token(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return user, client


@pytest.fixture
def admin_client(restaurant):
    return _staff_client("admin@test.dineos", "ADMIN", restaurant)


@pytest.fixture
def manager_client(restaurant):
    return _staff_client("manager@test.dineos", "MANAGER", restaurant)


@pytest.fixture
def server_client(restaurant):
    return _staff_client("server@test.dineos", "SERVER", restaurant)


@pytest.fixture
def cashier_client(restaurant):
    return _staff_client("cashier@test.dineos", "CASHIER", restaurant)


@pytest.fixture
def kds_device(restaurant):
    return KDSDevice.objects.create(restaurant=restaurant, label="Test KDS")


@pytest.fixture
def kds_client(kds_device):
    client = APIClient()
    client.credentials(HTTP_X_KDS_API_KEY=kds_device.api_key)
    return kds_device, client


@pytest.fixture
def table(restaurant):
    return Table.objects.create(restaurant=restaurant, table_number="5", capacity=4)


@pytest.fixture
def menu_item(restaurant):
    category = Category.objects.create(restaurant=restaurant, name="Main Course", sort_order=1)
    item = MenuItem.objects.create(category=category, name="Chicken Biryani", price=Decimal("220.00"))
    PreparedPortion.objects.create(menu_item=item, date=timezone.localdate(), portions_initial=20, portions_remaining=20)
    return item
