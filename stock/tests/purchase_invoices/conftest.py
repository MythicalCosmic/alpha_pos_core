from types import SimpleNamespace

import pytest

from base.models import User
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from stock.models import (
    StockItem,
    StockLevel,
    StockLocation,
    StockSettings,
    StockUnit,
    Supplier,
    SupplierStockItem,
    SupplierTransaction,
)
from stock.tests.test_warehouse_receiving import _client


@pytest.fixture
def invoice_data():
    warehouse = User.objects.create(
        first_name="Warehouse",
        last_name="User",
        email="invoice-warehouse@test.local",
        password="!",
        role="WAREHOUSE",
        status="ACTIVE",
        branch_id="branch1",
        permissions=DEFAULT_ROLE_PERMISSIONS["WAREHOUSE"],
    )
    manager = User.objects.create(
        first_name="Manager",
        last_name="User",
        email="invoice-manager@test.local",
        password="!",
        role="MANAGER",
        status="ACTIVE",
        branch_id="branch1",
        permissions=DEFAULT_ROLE_PERMISSIONS["MANAGER"],
    )
    kg = StockUnit.objects.create(
        name="Kilogram",
        short_name="kg",
        unit_type="WEIGHT",
        is_base_unit=True,
        decimal_places=3,
        branch_id="cloud",
    )
    location = StockLocation.objects.create(
        name="Main warehouse", type="WAREHOUSE", branch_id="branch1"
    )
    item = StockItem.objects.create(
        name="Donar meat",
        sku="INVOICE-MEAT",
        base_unit=kg,
        item_type="RAW",
        cost_price=77777,
        avg_cost_price=80000,
        last_cost_price=90000,
        branch_id="branch1",
    )
    level = StockLevel.objects.create(
        stock_item=item, location=location, quantity=10, branch_id="branch1"
    )
    supplier = Supplier.objects.create(
        name="Supplier", current_balance=200000, branch_id="branch1"
    )
    SupplierTransaction.objects.create(
        supplier=supplier,
        type="ADJUSTMENT",
        amount=200000,
        balance_before=0,
        balance_after=200000,
        reference_type="SyntheticOpening",
        branch_id="branch1",
    )
    link = SupplierStockItem.objects.create(
        supplier=supplier,
        stock_item=item,
        unit=kg,
        price=80000,
        price_is_known=True,
        price_source="CATALOG",
        branch_id="branch1",
    )
    settings = StockSettings.load()
    settings.stock_enabled = True
    settings.track_batches = False
    settings.save()
    payload = {
        "supplier_id": supplier.id,
        "location_id": location.id,
        "invoice_date": "2026-09-06",
        "supplier_invoice_number": "INV-104",
        "currency": "UZS",
        "declared_total_uzs": 500000,
        "notes": "",
        "lines": [
            {
                "supplier_item_id": link.id,
                "quantity": 5,
                "unit_price_uzs": 100000,
                "is_free": False,
                "free_reason": "",
                "batch_number": "",
                "expiry_date": None,
                "notes": "",
            }
        ],
    }
    return SimpleNamespace(
        warehouse=warehouse,
        manager=manager,
        kg=kg,
        location=location,
        item=item,
        level=level,
        supplier=supplier,
        link=link,
        payload=payload,
        client=_client(warehouse),
        manager_client=_client(manager),
    )
