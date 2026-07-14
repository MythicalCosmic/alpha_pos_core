"""Product ↔ stock linkage models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class ProductStockLink(SyncMixin, models.Model):
    """
    Links a POS product to either a recipe, a direct stock item, or
    a set of components. This drives automatic stock deduction on sale.
    """

    class LinkType(models.TextChoices):
        RECIPE = "RECIPE", "Recipe"
        DIRECT_ITEM = "DIRECT_ITEM", "Direct Item"
        COMPONENT_BASED = "COMPONENT_BASED", "Component Based"

    class DeductOn(models.TextChoices):
        CREATED = "CREATED", "Order Created"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        PAID = "PAID", "Paid"


    product = models.OneToOneField(
        "base.Product",
        on_delete=models.CASCADE,
        related_name="stock_link",
    )
    link_type = models.CharField(max_length=20, choices=LinkType.choices)
    recipe = models.ForeignKey(
        'stock.Recipe',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_links",
    )
    stock_item = models.ForeignKey(
        'stock.StockItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_links",
    )
    quantity_per_sale = models.DecimalField(
        max_digits=15, decimal_places=4, default=1
    )
    unit = models.ForeignKey(
        'stock.StockUnit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    deduct_on_status = models.CharField(
        max_length=20, choices=DeductOn.choices, default=DeductOn.PREPARING
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['product_uuid'] = str(self.product.uuid) if self.product else None
        data['recipe_uuid'] = str(self.recipe.uuid) if self.recipe else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return f"Link: Product#{self.product_id} → {self.get_link_type_display()}"


class ProductComponentStock(SyncMixin, models.Model):
    product_stock_link = models.ForeignKey(
        ProductStockLink, on_delete=models.CASCADE, related_name="components"
    )
    component_name = models.CharField(max_length=100)
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="+"
    )
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    is_default = models.BooleanField(default=True)
    is_addable = models.BooleanField(default=True)
    is_removable = models.BooleanField(default=True)
    price_modifier = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['product_stock_link_uuid'] = str(self.product_stock_link.uuid) if self.product_stock_link else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return self.component_name
