"""Load live recipe children and conversion metadata for a single read."""

from django.db.models import Prefetch, prefetch_related_objects

from stock.models import (
    RecipeByProduct,
    RecipeIngredient,
    RecipeIngredientSubstitute,
    RecipeStep,
)
from stock.services.conversions import UnitConversions


def load_recipe_children(
    recipes,
    *,
    include_ingredients=True,
    include_steps=False,
    include_byproducts=False,
    include_substitutes=False,
    include_cost=False,
):
    recipes = list(recipes)
    lookups = []
    if include_ingredients or include_cost:
        lookups.append(
            Prefetch(
                "ingredients",
                to_attr="_live_ingredients",
                queryset=RecipeIngredient.objects.filter(is_deleted=False)
                .select_related("stock_item__base_unit", "unit")
                .order_by("sort_order"),
            )
        )
    if include_steps:
        lookups.append(
            Prefetch(
                "steps",
                to_attr="_live_steps",
                queryset=RecipeStep.objects.filter(is_deleted=False).order_by(
                    "step_number"
                ),
            )
        )
    if include_byproducts:
        lookups.append(
            Prefetch(
                "by_products",
                to_attr="_live_byproducts",
                queryset=RecipeByProduct.objects.filter(
                    is_deleted=False
                ).select_related("stock_item", "unit"),
            )
        )
    prefetch_related_objects(recipes, *lookups)
    ingredients = [
        line for recipe in recipes for line in getattr(recipe, "_live_ingredients", [])
    ]
    if include_substitutes:
        prefetch_related_objects(
            ingredients,
            Prefetch(
                "substitutes",
                to_attr="_live_substitutes",
                queryset=RecipeIngredientSubstitute.objects.filter(is_deleted=False)
                .select_related("substitute_item", "unit")
                .order_by("priority"),
            ),
        )
    if include_cost:
        conversions = UnitConversions(
            [line.stock_item for line in ingredients]
            + [recipe.output_item for recipe in recipes],
            [line.unit for line in ingredients]
            + [recipe.output_unit for recipe in recipes],
        )
        for recipe in recipes:
            recipe._unit_conversions = conversions
    return recipes
