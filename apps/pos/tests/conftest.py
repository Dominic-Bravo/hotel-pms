from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.pos.models import DiningTable, MenuCategory, MenuItem, Outlet, Terminal


@pytest.fixture
def pos_context(db):
    User = get_user_model()
    staff = User.objects.create_user(
        email="jamie.cruz@example.com",
        password="testpassword123",
    )
    outlet = Outlet.objects.create(
        code="MAIN",
        name="Main Dining",
        type=Outlet.Type.RESTAURANT,
    )
    terminal = Terminal.objects.create(
        outlet=outlet,
        code="T01",
        name="Terminal 01",
    )
    table = DiningTable.objects.create(
        outlet=outlet,
        name="Table 1",
        seats=4,
    )
    category = MenuCategory.objects.create(
        outlet=outlet,
        name="Mains",
        sort_order=1,
    )
    steak = MenuItem.objects.create(
        category=category,
        sku="STK-001",
        name="Ribeye Steak",
        price=Decimal("800.00"),
        is_vatable=True,
        senior_pwd_eligible=True,
        station=MenuItem.Station.KITCHEN,
    )
    soda = MenuItem.objects.create(
        category=category,
        sku="DRK-001",
        name="Soda",
        price=Decimal("100.00"),
        is_vatable=True,
        senior_pwd_eligible=True,
        station=MenuItem.Station.BAR,
    )
    return {
        "staff": staff,
        "outlet": outlet,
        "terminal": terminal,
        "table": table,
        "category": category,
        "steak": steak,
        "soda": soda,
        "business_date": date(2026, 11, 27),
    }


@pytest.fixture
def api_auth_client(pos_context):
    client = APIClient()
    client.force_authenticate(user=pos_context["staff"])
    return client