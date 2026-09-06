"""Recipe child editing services."""

from decimal import Decimal
from typing import Any, Dict, List

from django.db import transaction

from base.helpers.response import ServiceResponse
from stock.repositories import (
    RecipeIngredientRepository,
    RecipeIngredientSubstituteRepository,
    RecipeRepository,
    StockItemRepository,
    StockUnitRepository,
)
from stock.services.base_service import to_decimal


class RecipeIngredientService:
    @classmethod
    def serialize(cls, ing, include_substitutes: bool = True) -> Dict[str, Any]:
        data = {
            "id": ing.id,
            "uuid": str(ing.uuid),
            "recipe_id": ing.recipe_id,
            "stock_item_id": ing.stock_item_id,
            "stock_item": {
                "id": ing.stock_item.id,
                "name": ing.stock_item.name,
                "sku": ing.stock_item.sku,
            },
            "quantity": str(ing.quantity),
            "unit_id": ing.unit_id,
            "unit": ing.unit.short_name,
            "is_optional": ing.is_optional,
            "is_scalable": ing.is_scalable,
            "waste_percentage": str(ing.waste_percentage),
            "prep_instructions": ing.prep_instructions,
            "sort_order": ing.sort_order,
            "substitute_group": ing.substitute_group,
        }

        if include_substitutes:
            substitutes = getattr(ing, "_live_substitutes", None)
            if substitutes is None:
                substitutes = RecipeIngredientSubstituteRepository.get_for_ingredient(
                    ing.id
                )
            data["substitutes"] = [
                RecipeIngredientSubstituteService.serialize(sub) for sub in substitutes
            ]

        return data

    @classmethod
    @transaction.atomic
    def add(
        cls,
        recipe_id: int,
        stock_item_id: int,
        quantity: Decimal,
        unit_id: int,
        is_optional: bool = False,
        is_scalable: bool = True,
        waste_percentage: Decimal = Decimal("0"),
        prep_instructions: str = "",
        sort_order: int = 0,
        substitute_group: str = "",
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

        if sort_order == 0:
            last = (
                RecipeIngredientRepository.model.objects.filter(recipe=recipe)
                .order_by("-sort_order")
                .first()
            )
            sort_order = (last.sort_order + 1) if last else 1

        ing = RecipeIngredientRepository.create(
            recipe=recipe,
            stock_item=stock_item,
            quantity=to_decimal(quantity),
            unit=unit,
            is_optional=is_optional,
            is_scalable=is_scalable,
            waste_percentage=to_decimal(waste_percentage),
            prep_instructions=prep_instructions,
            sort_order=sort_order,
            substitute_group=substitute_group,
        )

        return ServiceResponse.success(
            data={"id": ing.id, "ingredient": cls.serialize(ing)},
            message="Ingredient added",
        )

    @classmethod
    @transaction.atomic
    def update(cls, ingredient_id: int, **kwargs) -> Dict[str, Any]:
        ing = (
            RecipeIngredientRepository.model.objects.select_related(
                "stock_item", "unit"
            )
            .filter(id=ingredient_id)
            .first()
        )
        if not ing:
            return ServiceResponse.not_found("Ingredient not found")

        if "stock_item_id" in kwargs:
            stock_item = StockItemRepository.get_by_id(kwargs["stock_item_id"])
            if not stock_item:
                return ServiceResponse.not_found("Stock item not found")
            ing.stock_item = stock_item

        if "unit_id" in kwargs:
            unit = StockUnitRepository.get_by_id(kwargs["unit_id"])
            if not unit:
                return ServiceResponse.not_found("Unit not found")
            ing.unit = unit

        for field in [
            "quantity",
            "is_optional",
            "is_scalable",
            "waste_percentage",
            "prep_instructions",
            "sort_order",
            "substitute_group",
        ]:
            if field in kwargs:
                value = kwargs[field]
                if field in ["quantity", "waste_percentage"]:
                    value = to_decimal(value)
                setattr(ing, field, value)

        ing.save()

        return ServiceResponse.success(
            data={"ingredient": cls.serialize(ing)}, message="Ingredient updated"
        )

    @classmethod
    @transaction.atomic
    def remove(cls, ingredient_id: int) -> Dict[str, Any]:
        ing = RecipeIngredientRepository.get_by_id(ingredient_id)
        if not ing:
            return ServiceResponse.not_found("Ingredient not found")

        ing.delete()
        return ServiceResponse.success(message="Ingredient removed")

    @classmethod
    @transaction.atomic
    def reorder(cls, recipe_id: int, ingredient_ids: List[int]) -> Dict[str, Any]:
        RecipeIngredientRepository.reorder(recipe_id, ingredient_ids)

        return ServiceResponse.success(
            data={"reordered": len(ingredient_ids)}, message="Ingredients reordered"
        )


class RecipeIngredientSubstituteService:
    @classmethod
    def serialize(cls, sub) -> Dict[str, Any]:
        return {
            "id": sub.id,
            "uuid": str(sub.uuid),
            "recipe_ingredient_id": sub.recipe_ingredient_id,
            "substitute_item_id": sub.substitute_item_id,
            "substitute_item_name": sub.substitute_item.name,
            "quantity": str(sub.quantity),
            "unit": sub.unit.short_name,
            "conversion_note": sub.conversion_note,
            "priority": sub.priority,
        }

    @classmethod
    @transaction.atomic
    def add(
        cls,
        recipe_ingredient_id: int,
        substitute_item_id: int,
        quantity: Decimal,
        unit_id: int,
        conversion_note: str = "",
        priority: int = 1,
    ) -> Dict[str, Any]:
        recipe_ing = RecipeIngredientRepository.get_by_id(recipe_ingredient_id)
        if not recipe_ing:
            return ServiceResponse.not_found("Recipe ingredient not found")

        substitute_item = StockItemRepository.get_by_id(substitute_item_id)
        if not substitute_item:
            return ServiceResponse.not_found("Substitute item not found")

        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found("Unit not found")

        sub = RecipeIngredientSubstituteRepository.create(
            recipe_ingredient=recipe_ing,
            substitute_item=substitute_item,
            quantity=to_decimal(quantity),
            unit=unit,
            conversion_note=conversion_note,
            priority=priority,
        )

        return ServiceResponse.success(
            data={"id": sub.id, "substitute": cls.serialize(sub)},
            message="Substitute added",
        )

    @classmethod
    @transaction.atomic
    def remove(cls, substitute_id: int) -> Dict[str, Any]:
        sub = RecipeIngredientSubstituteRepository.get_by_id(substitute_id)
        if not sub:
            return ServiceResponse.not_found("Substitute not found")

        sub.delete()
        return ServiceResponse.success(message="Substitute removed")
