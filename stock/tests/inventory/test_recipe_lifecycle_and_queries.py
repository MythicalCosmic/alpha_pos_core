from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from stock.models import Recipe, RecipeIngredient, StockItem, StockLevel
from stock.services.recipe_service import RecipeService


@pytest.mark.parametrize("size", [1, 20])
def test_recipe_availability_has_bounded_queries(
    inventory_catalog, size, record_property
):
    c = inventory_catalog
    c.line.delete()
    for i in range(size):
        item = StockItem.objects.create(
            name=f"Recipe ingredient {i}",
            base_unit=c.gram,
            item_type="RAW",
            avg_cost_price=2,
        )
        RecipeIngredient.objects.create(
            recipe=c.recipe, stock_item=item, quantity=1, unit=c.kilogram
        )
        StockLevel.objects.create(stock_item=item, location=c.location, quantity=2000)
    with CaptureQueriesContext(connection) as queries:
        result, status = RecipeService.check_availability(
            c.recipe.id, location_id=c.location.id
        )
    record_property("sql_queries", len(queries))
    assert status == 200
    assert len(result["data"]["ingredients"]) == size
    assert all(row["is_available"] for row in result["data"]["ingredients"])
    assert len(queries) <= 7, f"{size} ingredients used {len(queries)} SQL queries"


def test_recipe_creation_rolls_back_earlier_children_on_failure(inventory_catalog):
    c = inventory_catalog
    before = (Recipe.objects.count(), RecipeIngredient.objects.count())
    result, status = RecipeService.create(
        name="Rejected recipe",
        code="REJECTED",
        output_item_id=c.output.id,
        output_quantity=1,
        output_unit_id=c.piece.id,
        ingredients=[
            {"stock_item_id": c.ingredient.id, "quantity": 1, "unit_id": c.gram.id},
            {"stock_item_id": 2**63 - 1, "quantity": 1, "unit_id": c.gram.id},
        ],
    )
    assert status == 404, result
    assert (Recipe.objects.count(), RecipeIngredient.objects.count()) == before


def test_approved_recipe_update_creates_unique_version_with_live_children(
    inventory_catalog,
):
    c = inventory_catalog
    c.recipe.approved_at = timezone.now()
    c.recipe.save(update_fields=["approved_at"])
    RecipeIngredient.objects.create(
        recipe=c.recipe,
        stock_item=c.ingredient,
        quantity=999,
        unit=c.gram,
        is_deleted=True,
    )
    result, status = RecipeService.update(c.recipe.id, output_quantity=20)
    assert status == 200, result
    new = Recipe.objects.get(pk=result["data"]["id"])
    assert new.id != c.recipe.id
    assert new.parent_recipe_id == c.recipe.id
    assert new.code != c.recipe.code
    assert new.version == 2
    assert new.output_quantity == Decimal("20")
    assert new.ingredients.count() == 1
    c.recipe.refresh_from_db()
    assert c.recipe.output_quantity == Decimal("10")


def test_unapproved_recipe_quantity_update_is_saved(inventory_catalog):
    c = inventory_catalog
    result, status = RecipeService.update(c.recipe.id, output_quantity=20)
    assert status == 200, result
    c.recipe.refresh_from_db()
    assert c.recipe.output_quantity == Decimal("20")


def test_recipe_parent_cycle_is_rejected_without_unbounded_reads(
    inventory_catalog, monkeypatch
):
    c = inventory_catalog
    Recipe.objects.filter(pk=c.recipe.pk).update(
        parent_recipe_id=c.recipe.id, approved_at=timezone.now()
    )
    descriptor = Recipe.parent_recipe
    calls = []

    def bounded_parent(instance):
        calls.append(True)
        assert len(calls) <= 8, "Recipe ancestry traversal repeated a cycle"
        return descriptor.__get__(instance, Recipe)

    monkeypatch.setattr(Recipe, "parent_recipe", property(bounded_parent))
    before = Recipe.objects.count()
    result, status = RecipeService.update(c.recipe.id, output_quantity=20)
    assert status == 400, result
    assert Recipe.objects.count() == before
