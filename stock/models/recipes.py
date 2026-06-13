"""Recipes models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class Recipe(SyncMixin, models.Model):
    class RecipeType(models.TextChoices):
        PRODUCTION = "PRODUCTION", "Production"
        ASSEMBLY = "ASSEMBLY", "Assembly"
        PREPARATION = "PREPARATION", "Preparation"
        DISASSEMBLY = "DISASSEMBLY", "Disassembly"


    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, unique=True, blank=True, null=True)

    output_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="recipes_as_output"
    )
    output_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    output_unit = models.ForeignKey(
        'stock.StockUnit', on_delete=models.PROTECT, related_name="+"
    )

    recipe_type = models.CharField(max_length=20, choices=RecipeType.choices)
    version = models.PositiveIntegerField(default=1)
    is_active_version = models.BooleanField(default=True)
    parent_recipe = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="versions",
    )

    yield_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=100
    )
    estimated_time_minutes = models.PositiveIntegerField(null=True, blank=True)
    difficulty_level = models.PositiveSmallIntegerField(
        default=1, help_text="1 (easy) to 5 (hard)"
    )
    production_location = models.ForeignKey(
        'stock.StockLocation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recipes",
    )
    instructions = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")

    is_scalable = models.BooleanField(default=True)
    min_batch_size = models.DecimalField(
        max_digits=15, decimal_places=4, default=1
    )
    max_batch_size = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )

    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_recipes",
    )
    approved_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_recipes",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ["name", "-version"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['output_item_uuid'] = str(self.output_item.uuid) if self.output_item else None
        data['output_unit_uuid'] = str(self.output_unit.uuid) if self.output_unit else None
        data['production_location_uuid'] = str(self.production_location.uuid) if self.production_location else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        data['parent_recipe_uuid'] = str(self.parent_recipe.uuid) if self.parent_recipe else None
        return data

    def __str__(self):
        return f"{self.name} v{self.version}"


class RecipeIngredient(SyncMixin, models.Model):

    recipe = models.ForeignKey(
        Recipe, on_delete=models.CASCADE, related_name="ingredients"
    )
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="used_in_recipes"
    )
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    is_optional = models.BooleanField(default=False)
    is_scalable = models.BooleanField(default=True)
    waste_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=0
    )
    prep_instructions = models.TextField(blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)
    substitute_group = models.CharField(max_length=50, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ["sort_order"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['recipe_uuid'] = str(self.recipe.uuid) if self.recipe else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return f"{self.stock_item.name} × {self.quantity}"


class RecipeIngredientSubstitute(SyncMixin, models.Model):

    recipe_ingredient = models.ForeignKey(
        RecipeIngredient, on_delete=models.CASCADE, related_name="substitutes"
    )
    substitute_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="substitute_for"
    )
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    conversion_note = models.TextField(blank=True, default="")
    priority = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ["priority"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['recipe_ingredient_uuid'] = str(self.recipe_ingredient.uuid) if self.recipe_ingredient else None
        data['substitute_item_uuid'] = str(self.substitute_item.uuid) if self.substitute_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return f"Sub: {self.substitute_item.name}"


class RecipeByProduct(SyncMixin, models.Model):

    recipe = models.ForeignKey(
        Recipe, on_delete=models.CASCADE, related_name="by_products"
    )
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="byproduct_of"
    )
    expected_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    is_waste = models.BooleanField(default=False)
    value_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=0
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['recipe_uuid'] = str(self.recipe.uuid) if self.recipe else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return f"{'Waste' if self.is_waste else 'By-product'}: {self.stock_item.name}"


class RecipeStep(SyncMixin, models.Model):

    recipe = models.ForeignKey(
        Recipe, on_delete=models.CASCADE, related_name="steps"
    )
    step_number = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    temperature = models.CharField(max_length=50, blank=True, default="")
    equipment_needed = models.TextField(blank=True, default="")
    is_checkpoint = models.BooleanField(default=False)
    photo_url = models.URLField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ["step_number"]
        unique_together = [("recipe", "step_number")]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['recipe_uuid'] = str(self.recipe.uuid) if self.recipe else None
        return data

    def __str__(self):
        return f"Step {self.step_number}: {self.title}"
