from decimal import Decimal

from stock.models import (
    ProductStockLink,
    RecipeIngredient,
    StockLevel,
    StockTransaction,
)
from stock.services.order_service import OrderStockService
from stock.services.recipe_service import RecipeService


def test_availability_converts_recipe_units_to_stock_base_units(inventory_catalog):
    c = inventory_catalog
    result, status = OrderStockService.check_availability(
        [{"product_id": c.product.id}], c.location.id
    )
    assert status == 200
    assert result["data"]["all_available"] is False
    shortage = result["data"]["items"][0]["shortages"][0]
    assert Decimal(shortage["required"]) == Decimal("100")
    assert Decimal(shortage["available"]) == Decimal("50")


def test_availability_combines_demands_from_different_products(
    inventory_catalog, category
):
    from base.models import Product

    c = inventory_catalog
    c.level.quantity = 150
    c.level.save(update_fields=["quantity"])
    second = Product.objects.create(
        name="Second flour product", price=10, category=category
    )
    ProductStockLink.objects.create(
        product=second,
        link_type="DIRECT_ITEM",
        stock_item=c.ingredient,
        quantity_per_sale=100,
        unit=c.gram,
    )
    result, status = OrderStockService.check_availability(
        [
            {"product_id": c.product.id},
            {"product_id": second.id},
        ],
        c.location.id,
    )
    assert status == 200
    assert result["data"]["all_available"] is False
    assert not all(row["available"] for row in result["data"]["items"])


def test_recipe_availability_combines_repeated_required_ingredients(inventory_catalog):
    c = inventory_catalog
    c.line.quantity = Decimal("0.03")
    c.line.save(update_fields=["quantity"])
    RecipeIngredient.objects.create(
        recipe=c.recipe, stock_item=c.ingredient, quantity=30, unit=c.gram
    )
    result, status = RecipeService.check_availability(
        c.recipe.id, location_id=c.location.id
    )
    assert status == 200
    assert not all(row["is_available"] for row in result["data"]["ingredients"])


def test_reservation_uses_base_units_and_release_restores_them(
    inventory_catalog, admin_user
):
    c = inventory_catalog
    c.level.quantity = 150
    c.level.save(update_fields=["quantity"])
    result, status = OrderStockService.reserve_for_order(
        1001,
        [{"product_id": c.product.id}],
        c.location.id,
        admin_user.id,
    )
    assert status == 200, result
    c.level.refresh_from_db()
    assert c.level.reserved_quantity == Decimal("100")
    movement = StockTransaction.objects.get(
        reference_id=1001, movement_type="RESERVATION"
    )
    assert movement.base_quantity == Decimal("100")
    assert movement.unit_id == c.gram.id
    result, status = OrderStockService.release_reservation(1001, admin_user.id)
    assert status == 200, result
    c.level.refresh_from_db()
    assert c.level.reserved_quantity == Decimal("0")


def test_insufficient_reservation_leaves_no_stock_writes(inventory_catalog, admin_user):
    c = inventory_catalog
    before = StockTransaction.objects.count()
    result, status = OrderStockService.reserve_for_order(
        1002,
        [{"product_id": c.product.id}],
        c.location.id,
        admin_user.id,
    )
    assert status == 400, result
    c.level.refresh_from_db()
    assert c.level.reserved_quantity == 0
    assert StockTransaction.objects.count() == before


def test_other_location_cannot_satisfy_recipe_requirement(inventory_catalog):
    c = inventory_catalog
    StockLevel.objects.create(stock_item=c.ingredient, location=c.other, quantity=10000)
    result, status = RecipeService.check_availability(
        c.recipe.id, location_id=c.location.id
    )
    assert status == 200
    assert result["data"]["ingredients"][0]["is_available"] is False


def test_item_conversion_override_is_used_and_refreshes_on_next_read(inventory_catalog):
    from stock.models import StockItemUnit

    c = inventory_catalog
    c.level.quantity = 150
    c.level.save(update_fields=["quantity"])
    override = StockItemUnit.objects.create(
        stock_item=c.ingredient,
        unit=c.kilogram,
        conversion_to_base=2000,
    )
    result, status = OrderStockService.check_availability(
        [{"product_id": c.product.id}], c.location.id
    )
    assert status == 200
    assert result["data"]["all_available"] is False
    assert Decimal(result["data"]["items"][0]["shortages"][0]["required"]) == 200
    override.conversion_to_base = 500
    override.save(update_fields=["conversion_to_base"])
    result, status = OrderStockService.check_availability(
        [{"product_id": c.product.id}], c.location.id
    )
    assert status == 200
    assert result["data"]["all_available"] is True
    assert RecipeService.calculate_cost(c.recipe.id) == 1000


def test_later_reservation_failure_rolls_back_earlier_ingredient(
    inventory_catalog, admin_user
):
    from stock.models import StockItem

    c = inventory_catalog
    c.level.quantity = 150
    c.level.save(update_fields=["quantity"])
    salt = StockItem.objects.create(
        name="Insufficient salt", base_unit=c.gram, item_type="RAW"
    )
    RecipeIngredient.objects.create(
        recipe=c.recipe, stock_item=salt, quantity=100, unit=c.gram
    )
    salt_level = StockLevel.objects.create(
        stock_item=salt, location=c.location, quantity=5
    )
    before = StockTransaction.objects.count()
    result, status = OrderStockService.reserve_for_order(
        1003,
        [{"product_id": c.product.id}],
        c.location.id,
        admin_user.id,
    )
    assert status == 400, result
    c.level.refresh_from_db()
    salt_level.refresh_from_db()
    assert c.level.reserved_quantity == salt_level.reserved_quantity == 0
    assert StockTransaction.objects.count() == before
