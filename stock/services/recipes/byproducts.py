"""Recipe child editing services."""

from decimal import Decimal
from typing import Any, Dict

from django.db import transaction

from base.helpers.response import ServiceResponse
from stock.repositories import (
    RecipeByProductRepository,
    RecipeRepository,
    StockItemRepository,
    StockUnitRepository,
)
from stock.services.base_service import to_decimal


class RecipeByProductService:
    @classmethod
    def serialize(cls, bp) -> Dict[str, Any]:
        return {
            "id": bp.id,
            "uuid": str(bp.uuid),
            "recipe_id": bp.recipe_id,
            "stock_item_id": bp.stock_item_id,
            "stock_item_name": bp.stock_item.name,
            "expected_quantity": str(bp.expected_quantity),
            "unit": bp.unit.short_name,
            "is_waste": bp.is_waste,
            "value_percentage": str(bp.value_percentage),
        }

    @classmethod
    @transaction.atomic
    def add(
        cls,
        recipe_id: int,
        stock_item_id: int,
        expected_quantity: Decimal,
        unit_id: int,
        is_waste: bool = False,
        value_percentage: Decimal = Decimal("0"),
    ) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found("Stock item not found")

        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found("Unit not found")

        bp = RecipeByProductRepository.create(
            recipe=recipe,
            stock_item=stock_item,
            expected_quantity=to_decimal(expected_quantity),
            unit=unit,
            is_waste=is_waste,
            value_percentage=to_decimal(value_percentage),
        )

        return ServiceResponse.success(
            data={"id": bp.id, "by_product": cls.serialize(bp)},
            message="By-product added",
        )

    @classmethod
    @transaction.atomic
    def remove(cls, byproduct_id: int) -> Dict[str, Any]:
        bp = RecipeByProductRepository.get_by_id(byproduct_id)
        if not bp:
            return ServiceResponse.not_found("By-product not found")

        bp.delete()
        return ServiceResponse.success(message="By-product removed")
