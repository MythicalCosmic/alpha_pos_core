from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from stock.models import StockItem, StockLevel
from stock.services.item_service import StockItemService
from stock.services.level_service import StockLevelService


def test_low_stock_filter_alias_respects_location_and_live_levels(inventory_catalog):
    c = inventory_catalog
    c.level.quantity = 5
    c.level.save(update_fields=["quantity"])
    StockLevel.objects.create(stock_item=c.ingredient, location=c.other, quantity=10000)
    result, status = StockItemService.list(
        low_stock_only=True, location_id=c.location.id
    )
    assert status == 200
    assert [row["id"] for row in result["data"]["items"]] == [c.ingredient.id]


def test_low_stock_ignores_deleted_levels(inventory_catalog):
    c = inventory_catalog
    c.level.quantity = 5
    c.level.save(update_fields=["quantity"])
    StockLevel.objects.create(
        stock_item=c.ingredient, location=c.other, quantity=10000, is_deleted=True
    )
    result, status = StockItemService.list(low_stock=True)
    assert status == 200
    assert c.ingredient.id in {row["id"] for row in result["data"]["items"]}


def test_stock_level_search_filters_results(inventory_catalog):
    c = inventory_catalog
    StockLevel.objects.create(stock_item=c.output, location=c.location, quantity=1)
    result, status = StockLevelService.get_all(search="Test flour")
    assert status == 200
    assert [row["stock_item_id"] for row in result["data"]["levels"]] == [
        c.ingredient.id
    ]


@pytest.mark.parametrize("size", [1, 20])
def test_inventory_page_has_bounded_queries(inventory_catalog, size, record_property):
    c = inventory_catalog
    for i in range(size):
        item = StockItem.objects.create(
            name=f"Query item {i:02}",
            sku=f"QUERY-{i}",
            base_unit=c.gram,
            item_type="RAW",
        )
        StockLevel.objects.create(
            stock_item=item, location=c.location, quantity=8, reserved_quantity=3
        )
        StockLevel.objects.create(stock_item=item, location=c.other, quantity=999)
    with CaptureQueriesContext(connection) as queries:
        result, status = StockItemService.list(
            search="Query item",
            per_page=30,
            include_levels=True,
            location_id=c.location.id,
        )
    record_property("sql_queries", len(queries))
    assert status == 200
    assert len(result["data"]["items"]) == size
    assert all(
        Decimal(row["total_stock"]) == 8 and Decimal(row["total_reserved"]) == 3
        for row in result["data"]["items"]
    )
    assert len(queries) <= 3, f"{size} items used {len(queries)} SQL queries"
