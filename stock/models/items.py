"""Items models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class StockItem(SyncMixin, models.Model):
    class ItemType(models.TextChoices):
        RAW = "RAW", "Raw Material"
        SEMI = "SEMI", "Semi-Finished"
        FINISHED = "FINISHED", "Finished Good"
        PACKAGING = "PACKAGING", "Packaging"


    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=50, unique=True, blank=True, null=True)
    barcode = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    category = models.ForeignKey(
        'stock.StockCategory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
    )
    base_unit = models.ForeignKey(
        'stock.StockUnit',
        on_delete=models.PROTECT,
        related_name="stock_items",
    )
    item_type = models.CharField(max_length=20, choices=ItemType.choices)

    # Stock thresholds
    min_stock_level = models.DecimalField(max_digits=15, decimal_places=4, default=0)
    max_stock_level = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    reorder_point = models.DecimalField(max_digits=15, decimal_places=4, default=0)

    # Cost tracking
    cost_price = models.DecimalField(max_digits=15, decimal_places=4, default=0)
    avg_cost_price = models.DecimalField(max_digits=15, decimal_places=4, default=0)
    last_cost_price = models.DecimalField(max_digits=15, decimal_places=4, default=0)

    # Flags
    is_purchasable = models.BooleanField(default=True)
    is_sellable = models.BooleanField(default=False)
    is_producible = models.BooleanField(default=False)
    track_batches = models.BooleanField(default=False)
    track_expiry = models.BooleanField(default=False)
    default_expiry_days = models.PositiveIntegerField(null=True, blank=True)
    storage_conditions = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ["name"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['stock_category_uuid'] = str(self.category.uuid) if self.category else None
        data['base_unit_uuid'] = str(self.base_unit.uuid) if self.base_unit else None
        return data

    def __str__(self):
        return self.name


class StockItemUnit(SyncMixin, models.Model):
    """
    Alternative units for a stock item.
    E.g. a flour item's base unit is gram but it can also be tracked in kg or bags.
    """


    stock_item = models.ForeignKey(
        StockItem, on_delete=models.CASCADE, related_name="alternative_units"
    )
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT)
    is_default = models.BooleanField(default=False)
    conversion_to_base = models.DecimalField(
        max_digits=15,
        decimal_places=6,
        help_text="Multiply qty in this unit by this factor to get base unit qty",
    )
    barcode = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    class Meta:
        unique_together = [("stock_item", "unit")]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return f"{self.stock_item.name} – {self.unit.short_name}"
