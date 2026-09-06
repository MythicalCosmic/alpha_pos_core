from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from stock.models import (
    Recipe,
    RecipeIngredient,
    RecipeIngredientSubstitute,
    StockItem,
    StockLevel,
    Supplier,
    SupplierStockItem,
)
from stock.services.ai_assistant_service import AIStockAssistant
from stock.services.recipe_service import RecipeService


def _add_ingredients(catalog, size, *, substitutes=False):
    catalog.line.delete()
    for index in range(size):
        item = StockItem.objects.create(
            name=f"Read ingredient {index}",
            base_unit=catalog.gram,
            item_type="RAW",
            avg_cost_price=2,
        )
        line = RecipeIngredient.objects.create(
            recipe=catalog.recipe,
            stock_item=item,
            quantity=1,
            unit=catalog.kilogram,
        )
        StockLevel.objects.create(
            stock_item=item, location=catalog.location, quantity=2000
        )
        if substitutes:
            RecipeIngredientSubstitute.objects.create(
                recipe_ingredient=line,
                substitute_item=catalog.ingredient,
                quantity=1000,
                unit=catalog.gram,
            )
            RecipeIngredientSubstitute.objects.create(
                recipe_ingredient=line,
                substitute_item=catalog.ingredient,
                quantity=9999,
                unit=catalog.gram,
                is_deleted=True,
            )


@pytest.mark.parametrize("size", [1, 20])
def test_recipe_details_batch_live_children_and_cost(
    inventory_catalog, size, record_property
):
    c = inventory_catalog
    _add_ingredients(c, size, substitutes=True)
    with CaptureQueriesContext(connection) as queries:
        result, status = RecipeService.get(c.recipe.id)
    record_property("sql_queries", len(queries))
    assert status == 200, result
    recipe = result["data"]["recipe"]
    assert recipe["ingredient_count"] == size
    assert Decimal(recipe["estimated_cost"]) == Decimal(2000 * size)
    assert all(len(row["substitutes"]) == 1 for row in recipe["ingredients"])
    assert len(queries) <= 7, f"{size} ingredients used {len(queries)} queries"


@pytest.mark.parametrize("size", [1, 20])
def test_stock_snapshot_has_bounded_recipe_queries(
    inventory_catalog, size, record_property
):
    c = inventory_catalog
    _add_ingredients(c, size)
    with CaptureQueriesContext(connection) as queries:
        snapshot = AIStockAssistant._get_all_stock_data(c.location.id)
    record_property("sql_queries", len(queries))
    assert "error" not in snapshot, snapshot
    recipe = snapshot["recipes"][0]
    assert len(recipe["ingredients"]) == size
    assert recipe["total_cost_uzs"] == 2000 * size
    assert recipe["cost_per_unit_uzs"] == 200 * size
    assert recipe["can_produce"] is True
    assert len(queries) <= 18, f"{size} ingredients used {len(queries)} queries"


def test_stock_snapshot_combines_repeated_required_ingredients(inventory_catalog):
    c = inventory_catalog
    c.line.quantity = Decimal("0.03")
    c.line.save(update_fields=["quantity"])
    RecipeIngredient.objects.create(
        recipe=c.recipe, stock_item=c.ingredient, quantity=30, unit=c.gram
    )
    StockLevel.objects.create(stock_item=c.ingredient, location=c.other, quantity=10000)
    snapshot = AIStockAssistant._get_all_stock_data(c.location.id)
    recipe = snapshot["recipes"][0]
    assert recipe["can_produce"] is False
    assert all(row["available"] == 50 for row in recipe["ingredients"])


def test_optional_ingredient_does_not_consume_mandatory_availability(inventory_catalog):
    c = inventory_catalog
    c.line.quantity = Decimal("0.03")
    c.line.save(update_fields=["quantity"])
    RecipeIngredient.objects.create(
        recipe=c.recipe,
        stock_item=c.ingredient,
        quantity=100,
        unit=c.gram,
        is_optional=True,
    )
    result, status = RecipeService.check_availability(
        c.recipe.id, location_id=c.location.id
    )
    assert status == 200
    assert all(row["is_available"] for row in result["data"]["ingredients"])
    assert (
        AIStockAssistant._get_all_stock_data(c.location.id)["recipes"][0]["can_produce"]
        is True
    )


def test_stock_snapshot_batches_and_limits_supplier_items(
    inventory_catalog, record_property
):
    c = inventory_catalog
    items = [
        StockItem.objects.create(
            name=f"Supplier item {i}", base_unit=c.gram, item_type="RAW"
        )
        for i in range(12)
    ]
    for index in range(20):
        supplier = Supplier.objects.create(
            name=f"Supplier {index}", code=f"TEST-SUP-{index}"
        )
        for item in items:
            SupplierStockItem.objects.create(
                supplier=supplier, stock_item=item, unit=c.gram, price=1
            )
    with CaptureQueriesContext(connection) as queries:
        snapshot = AIStockAssistant._get_all_stock_data(c.location.id)
    record_property("sql_queries", len(queries))
    assert len(snapshot["suppliers"]) == 20
    assert all(len(row["items"]) == 10 for row in snapshot["suppliers"])
    assert len(queries) <= 18, f"20 suppliers used {len(queries)} queries"


@pytest.mark.parametrize(
    "entry_point",
    ["list", "search", "get_for_item", "get_versions", "get", "get_active_for_item"],
)
def test_deleted_recipe_is_excluded_from_catalog_reads(inventory_catalog, entry_point):
    c = inventory_catalog
    obsolete = Recipe.objects.create(
        name="AAAA obsolete recipe",
        code="OBSOLETE-RECIPE",
        output_item=c.output,
        output_unit=c.piece,
        output_quantity=1,
        recipe_type="PRODUCTION",
        parent_recipe=c.recipe,
        version=2,
        is_deleted=True,
    )
    if entry_point == "get":
        result, status = RecipeService.get(obsolete.id)
        assert status == 404, result
    elif entry_point == "get_active_for_item":
        assert RecipeService.get_active_for_item(c.output.id).id == c.recipe.id
    else:
        args = {
            "list": (),
            "search": ("AAAA",),
            "get_for_item": (c.output.id,),
            "get_versions": (c.recipe.id,),
        }[entry_point]
        result, status = getattr(RecipeService, entry_point)(*args)
        assert status == 200, result
        rows = result["data"][
            "versions" if entry_point == "get_versions" else "recipes"
        ]
        assert obsolete.id not in [row["id"] for row in rows]
