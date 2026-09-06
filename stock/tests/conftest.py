from decimal import Decimal
from types import SimpleNamespace

import pytest


@pytest.fixture
def inventory_catalog(db, product):
    from stock.models import (
        ProductStockLink,
        Recipe,
        RecipeIngredient,
        StockItem,
        StockLevel,
        StockLocation,
        StockSettings,
        StockUnit,
    )

    gram = StockUnit.objects.create(
        name="Test gram",
        short_name="tg",
        unit_type="WEIGHT",
        is_base_unit=True,
        decimal_places=4,
    )
    kilogram = StockUnit.objects.create(
        name="Test kilogram",
        short_name="tkg",
        unit_type="WEIGHT",
        base_unit=gram,
        conversion_factor=1000,
        decimal_places=4,
    )
    piece = StockUnit.objects.create(
        name="Test piece",
        short_name="tpc",
        unit_type="COUNT",
        is_base_unit=True,
        decimal_places=4,
    )
    location = StockLocation.objects.create(
        name="Test kitchen", type="KITCHEN", branch_id="branch-a"
    )
    other = StockLocation.objects.create(
        name="Other kitchen", type="KITCHEN", branch_id="branch-b"
    )
    ingredient = StockItem.objects.create(
        name="Test flour",
        sku="TEST-FLOUR",
        base_unit=gram,
        item_type="RAW",
        avg_cost_price=Decimal("2"),
        reorder_point=Decimal("10"),
    )
    output = StockItem.objects.create(
        name="Test bread", sku="TEST-BREAD", base_unit=piece, item_type="FINISHED"
    )
    recipe = Recipe.objects.create(
        name="Test bread recipe",
        code="TEST-BREAD-RECIPE",
        output_item=output,
        output_unit=piece,
        output_quantity=10,
        recipe_type="PRODUCTION",
        production_location=location,
    )
    line = RecipeIngredient.objects.create(
        recipe=recipe, stock_item=ingredient, quantity=1, unit=kilogram
    )
    ProductStockLink.objects.create(
        product=product,
        link_type="RECIPE",
        recipe=recipe,
        quantity_per_sale=1,
        unit=piece,
    )
    level = StockLevel.objects.create(
        stock_item=ingredient,
        location=location,
        quantity=50,
        branch_id="branch-a",
    )
    config = StockSettings.load()
    config.stock_enabled = True
    config.auto_deduct_on_sale = True
    config.reserve_on_order_create = True
    config.allow_negative_stock = False
    config.save()
    return SimpleNamespace(
        gram=gram,
        kilogram=kilogram,
        piece=piece,
        location=location,
        other=other,
        ingredient=ingredient,
        output=output,
        recipe=recipe,
        line=line,
        level=level,
        product=product,
        config=config,
    )
