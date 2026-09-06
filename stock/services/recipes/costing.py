"""Recipe quantities, costs, and availability on the RecipeService API."""

from decimal import Decimal
from typing import Any, Dict, List

from django.db import transaction

from base.helpers.response import ServiceResponse
from stock.models import Recipe
from stock.repositories import (
    RecipeIngredientRepository,
    RecipeRepository,
    StockLevelRepository,
)
from stock.services.base_service import round_decimal, to_decimal
from stock.services.conversions import UnitConversions


class RecipeCostingMixin:
    @classmethod
    def ingredient_cost_breakdown(
        cls, recipe: Recipe, batch_multiplier: Decimal = Decimal("1")
    ) -> List[Dict[str, Any]]:
        """Canonical live-ingredient requirements and costs for a recipe batch.

        Quantities are converted into each item's base unit (the unit used by
        ``StockLevel`` and ``avg_cost_price``), include configured ingredient
        waste, respect non-scalable ingredients, and exclude soft-deleted rows.
        """
        if not recipe:
            return []
        multiplier = to_decimal(batch_multiplier, Decimal("1"))
        ingredients = getattr(
            recipe, "_live_ingredients", getattr(recipe, "_ai_live_ingredients", None)
        )
        if ingredients is None:
            ingredients = RecipeIngredientRepository.get_for_recipe(recipe.id)
        ingredients = list(ingredients)
        conversions = getattr(recipe, "_unit_conversions", None)
        if conversions is None:
            conversions = UnitConversions.for_ingredients(ingredients)

        rows = []
        for ing in ingredients:
            if (
                getattr(ing, "is_deleted", False)
                or ing.stock_item.is_deleted
                or not ing.stock_item.is_active
            ):
                continue
            ingredient_quantity = to_decimal(ing.quantity)
            quantity = (
                ingredient_quantity * multiplier
                if ing.is_scalable
                else ingredient_quantity
            )
            quantity_with_waste = quantity
            waste_percentage = to_decimal(ing.waste_percentage)
            if waste_percentage > 0:
                quantity_with_waste *= 1 + waste_percentage / 100
            base_quantity = conversions.convert(
                ing.stock_item_id,
                quantity_with_waste,
                ing.unit_id,
            )
            cost = base_quantity * to_decimal(ing.stock_item.avg_cost_price)
            rows.append(
                {
                    "ingredient": ing,
                    "quantity": quantity,
                    "quantity_with_waste": quantity_with_waste,
                    "base_quantity": base_quantity,
                    "cost": cost,
                }
            )
        return rows

    @classmethod
    def _recipe_cost_total(
        cls, recipe: Recipe, batch_multiplier: Decimal = Decimal("1")
    ) -> Decimal:
        """Unrounded batch cost for downstream portion arithmetic."""
        return sum(
            (
                row["cost"]
                for row in cls.ingredient_cost_breakdown(
                    recipe,
                    batch_multiplier,
                )
            ),
            Decimal("0"),
        )

    @classmethod
    def calculate_recipe_cost(
        cls, recipe: Recipe, batch_multiplier: Decimal = Decimal("1")
    ) -> Decimal:
        return round_decimal(cls._recipe_cost_total(recipe, batch_multiplier), 2)

    @classmethod
    def effective_output_quantity(
        cls, recipe: Recipe, batch_multiplier: Decimal = Decimal("1")
    ) -> Decimal:
        """Actual output in ``recipe.output_unit`` after the yield percentage."""
        if not recipe:
            return Decimal("0")
        output = to_decimal(recipe.output_quantity) * to_decimal(
            batch_multiplier,
            Decimal("1"),
        )
        yield_pct = max(
            to_decimal(recipe.yield_percentage, Decimal("100")), Decimal("0")
        )
        return output * yield_pct / Decimal("100")

    @classmethod
    def calculate_portion_cost(
        cls, recipe: Recipe, quantity: Decimal = Decimal("1"), unit_id: int = None
    ) -> Decimal:
        """Cost for ``quantity`` of recipe output, with unit/yield conversion."""
        portion_ratio = cls.output_portion_ratio(recipe, quantity, unit_id)
        if portion_ratio <= 0:
            return Decimal("0")
        return round_decimal(
            cls._recipe_cost_total(recipe) * portion_ratio,
            2,
        )

    @classmethod
    def output_portion_ratio(
        cls, recipe: Recipe, quantity: Decimal = Decimal("1"), unit_id: int = None
    ) -> Decimal:
        """Fraction of one effective recipe batch represented by an output amount."""
        if not recipe:
            return Decimal("0")
        from ..unit_service import StockItemUnitService

        effective_output = cls.effective_output_quantity(recipe)
        if effective_output <= 0:
            return Decimal("0")
        conversions = getattr(recipe, "_unit_conversions", None)
        portion_unit_id = unit_id or recipe.output_unit_id
        convert = StockItemUnitService.convert_for_item
        if conversions is not None and portion_unit_id in conversions.units:
            convert = conversions.convert
        output_base = convert(
            recipe.output_item_id, effective_output, recipe.output_unit_id
        )
        portion_base = convert(
            recipe.output_item_id, to_decimal(quantity), portion_unit_id
        )
        if output_base <= 0:
            return Decimal("0")
        return portion_base / output_base

    @classmethod
    def calculate_cost(
        cls, recipe_id: int, batch_multiplier: Decimal = Decimal("1")
    ) -> Decimal:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return Decimal("0")
        return cls.calculate_recipe_cost(recipe, batch_multiplier)

    @classmethod
    @transaction.atomic
    def check_availability(
        cls,
        recipe_id: int,
        quantity: Decimal = Decimal("1"),
        location_id: int = None,
        batch_multiplier: Decimal = Decimal("1"),
    ) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        breakdown = cls.ingredient_cost_breakdown(recipe, batch_multiplier)
        totals = {}
        for row in breakdown:
            if not row["ingredient"].is_optional:
                item_id = row["ingredient"].stock_item_id
                totals[item_id] = (
                    totals.get(item_id, Decimal("0")) + row["base_quantity"]
                )
        available = StockLevelRepository.available_quantities(
            {row["ingredient"].stock_item_id for row in breakdown},
            location_id,
        )
        availability = []
        for row in breakdown:
            ing = row["ingredient"]
            available_stock = available.get(ing.stock_item_id, Decimal("0"))
            is_available = (
                ing.is_optional or available_stock >= totals[ing.stock_item_id]
            )

            availability.append(
                {
                    "stock_item_id": ing.stock_item_id,
                    "stock_item_name": ing.stock_item.name,
                    "required_quantity": str(
                        round_decimal(
                            row["quantity_with_waste"],
                            4,
                        )
                    ),
                    "required_base_quantity": str(
                        round_decimal(
                            row["base_quantity"],
                            4,
                        )
                    ),
                    "available_stock": str(round_decimal(available_stock, 4)),
                    "unit": ing.unit.short_name,
                    "base_unit": ing.stock_item.base_unit.short_name,
                    "is_optional": ing.is_optional,
                    "is_available": is_available,
                }
            )

        return ServiceResponse.success(
            data={
                "recipe_id": recipe.id,
                "recipe_name": recipe.name,
                "batch_multiplier": str(batch_multiplier),
                "ingredients": availability,
            }
        )
