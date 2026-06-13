"""Production orders models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class ProductionOrder(SyncMixin, models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PLANNED = "PLANNED", "Planned"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELED = "CANCELED", "Canceled"
        ON_HOLD = "ON_HOLD", "On Hold"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        NORMAL = "NORMAL", "Normal"
        HIGH = "HIGH", "High"
        URGENT = "URGENT", "Urgent"


    order_number = models.CharField(max_length=50, unique=True)
    recipe = models.ForeignKey(
        'stock.Recipe', on_delete=models.PROTECT, related_name="production_orders"
    )
    batch_multiplier = models.DecimalField(
        max_digits=10, decimal_places=4, default=1
    )
    expected_output_qty = models.DecimalField(max_digits=15, decimal_places=4)
    actual_output_qty = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    output_unit = models.ForeignKey(
        'stock.StockUnit', on_delete=models.PROTECT, related_name="+"
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    priority = models.CharField(
        max_length=10, choices=Priority.choices, default=Priority.NORMAL
    )

    source_location = models.ForeignKey(
        'stock.StockLocation',
        on_delete=models.PROTECT,
        related_name="production_orders_source",
    )
    output_location = models.ForeignKey(
        'stock.StockLocation',
        on_delete=models.PROTECT,
        related_name="production_orders_output",
    )

    planned_start = models.DateTimeField(null=True, blank=True)
    planned_end = models.DateTimeField(null=True, blank=True)
    actual_start = models.DateTimeField(null=True, blank=True)
    actual_end = models.DateTimeField(null=True, blank=True)

    assigned_to = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_production_orders",
    )
    created_by = models.ForeignKey(
        'base.User',
        on_delete=models.PROTECT,
        related_name="created_production_orders",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ["-created_at"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['recipe_uuid'] = str(self.recipe.uuid) if self.recipe else None
        data['output_unit_uuid'] = str(self.output_unit.uuid) if self.output_unit else None
        data['source_location_uuid'] = str(self.source_location.uuid) if self.source_location else None
        data['output_location_uuid'] = str(self.output_location.uuid) if self.output_location else None
        data['assigned_to_uuid'] = str(self.assigned_to.uuid) if self.assigned_to else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        return data

    def __str__(self):
        return f"PROD-{self.order_number}"


class ProductionOrderIngredient(SyncMixin, models.Model):
    class IngredientStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ALLOCATED = "ALLOCATED", "Allocated"
        CONSUMED = "CONSUMED", "Consumed"


    production_order = models.ForeignKey(
        ProductionOrder, on_delete=models.CASCADE, related_name="ingredients"
    )
    recipe_ingredient = models.ForeignKey(
        'stock.RecipeIngredient',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="+"
    )
    planned_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    actual_quantity = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    batch_used = models.ForeignKey(
        'stock.StockBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    variance = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    variance_reason = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=IngredientStatus.choices,
        default=IngredientStatus.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['production_order_uuid'] = str(self.production_order.uuid) if self.production_order else None
        data['recipe_ingredient_uuid'] = str(self.recipe_ingredient.uuid) if self.recipe_ingredient else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        data['batch_used_uuid'] = str(self.batch_used.uuid) if self.batch_used else None
        return data

    def __str__(self):
        return f"{self.stock_item.name} (planned: {self.planned_quantity})"


class ProductionOrderOutput(SyncMixin, models.Model):
    class QualityStatus(models.TextChoices):
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"
        PENDING = "PENDING", "Pending"


    production_order = models.ForeignKey(
        ProductionOrder, on_delete=models.CASCADE, related_name="outputs"
    )
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="+"
    )
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    is_primary_output = models.BooleanField(default=True)
    is_byproduct = models.BooleanField(default=False)
    is_waste = models.BooleanField(default=False)
    batch_created = models.ForeignKey(
        'stock.StockBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    quality_status = models.CharField(
        max_length=20,
        choices=QualityStatus.choices,
        default=QualityStatus.PENDING,
    )
    quality_notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['production_order_uuid'] = str(self.production_order.uuid) if self.production_order else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        data['batch_created_uuid'] = str(self.batch_created.uuid) if self.batch_created else None
        return data

    def __str__(self):
        label = "Primary" if self.is_primary_output else "By-product"
        return f"{label}: {self.stock_item.name} × {self.quantity}"


class ProductionOrderStep(SyncMixin, models.Model):
    class StepStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETED = "COMPLETED", "Completed"
        SKIPPED = "SKIPPED", "Skipped"


    production_order = models.ForeignKey(
        ProductionOrder, on_delete=models.CASCADE, related_name="steps"
    )
    recipe_step = models.ForeignKey(
        'stock.RecipeStep', on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(
        max_length=20, choices=StepStatus.choices, default=StepStatus.PENDING
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    notes = models.TextField(blank=True, default="")
    checkpoint_passed = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['production_order_uuid'] = str(self.production_order.uuid) if self.production_order else None
        data['recipe_step_uuid'] = str(self.recipe_step.uuid) if self.recipe_step else None
        data['completed_by_uuid'] = str(self.completed_by.uuid) if self.completed_by else None
        return data

    def __str__(self):
        return f"Step {self.recipe_step.step_number}: {self.get_status_display()}"
