"""Compatibility imports for the recipe services package.

Existing integrations keep importing these classes from this module.
Implementations live in stock.services.recipes.
"""

from .recipes import (
    RecipeByProductService,
    RecipeIngredientService,
    RecipeIngredientSubstituteService,
    RecipeService,
    RecipeStepService,
)

__all__ = [
    "RecipeService",
    "RecipeIngredientService",
    "RecipeIngredientSubstituteService",
    "RecipeByProductService",
    "RecipeStepService",
]
