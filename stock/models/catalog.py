"""Catalog (locations, units, categories) models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class StockLocation(SyncMixin, models.Model):
    class LocationType(models.TextChoices):
        WAREHOUSE = "WAREHOUSE", "Warehouse"
        KITCHEN = "KITCHEN", "Kitchen"
        BAR = "BAR", "Bar"
        STORAGE = "STORAGE", "Storage"
        PREP = "PREP", "Prep Area"


    name = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=LocationType.choices)
    parent_location = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    is_default = models.BooleanField(default=False)
    is_production_area = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ["sort_order", "name"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['parent_location_uuid'] = str(self.parent_location.uuid) if self.parent_location else None
        return data

    def __str__(self):
        return f"{self.name} ({self.get_type_display()})"


class StockUnit(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
    class UnitType(models.TextChoices):
        WEIGHT = "WEIGHT", "Weight"
        VOLUME = "VOLUME", "Volume"
        COUNT = "COUNT", "Count"
        LENGTH = "LENGTH", "Length"
        TIME = "TIME", "Time"


    name = models.CharField(max_length=50)
    short_name = models.CharField(max_length=10)
    unit_type = models.CharField(max_length=20, choices=UnitType.choices)
    is_base_unit = models.BooleanField(default=False)
    base_unit = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="derived_units",
        help_text="The base unit this unit converts to (e.g. gram for kilogram)",
    )
    conversion_factor = models.DecimalField(
        max_digits=15,
        decimal_places=6,
        default=1,
        help_text="Multiply by this factor to convert to base unit",
    )
    decimal_places = models.PositiveSmallIntegerField(
        default=2,
        help_text="Number of decimal places to display for this unit",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        ordering = ["unit_type", "name"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['base_unit_uuid'] = str(self.base_unit.uuid) if self.base_unit else None
        return data

    def __str__(self):
        return f"{self.name} ({self.short_name})"


class StockCategory(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
    class CategoryType(models.TextChoices):
        RAW_MATERIAL = "RAW_MATERIAL", "Raw Material"
        SEMI_FINISHED = "SEMI_FINISHED", "Semi-Finished"
        FINISHED_GOOD = "FINISHED_GOOD", "Finished Good"
        PACKAGING = "PACKAGING", "Packaging"
        CONSUMABLE = "CONSUMABLE", "Consumable"


    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    type = models.CharField(max_length=20, choices=CategoryType.choices)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        verbose_name_plural = "stock categories"
        ordering = ["sort_order", "name"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['parent_uuid'] = str(self.parent.uuid) if self.parent else None
        return data

    def __str__(self):
        return self.name
