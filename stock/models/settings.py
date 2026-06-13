"""Settings (app + alerts) models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class StockSettings(SyncMixin, models.Model):
    """
    Singleton settings table. Use StockSettings.load() to get the instance.
    """

    # Master controls
    stock_enabled = models.BooleanField(default=False)
    production_enabled = models.BooleanField(default=False)
    purchasing_enabled = models.BooleanField(default=False)
    multi_location_enabled = models.BooleanField(default=False)

    # Tracking options
    track_cost = models.BooleanField(default=True)
    track_batches = models.BooleanField(default=False)
    track_expiry = models.BooleanField(default=False)
    track_serial_numbers = models.BooleanField(default=False)

    # Behavior
    allow_negative_stock = models.BooleanField(default=False)
    auto_deduct_on_sale = models.BooleanField(default=True)
    deduct_on_order_status = models.CharField(max_length=20, default="PREPARING")
    reserve_on_order_create = models.BooleanField(default=False)
    auto_create_production = models.BooleanField(default=False)

    # Costing
    class CostingMethod(models.TextChoices):
        FIFO = "FIFO", "First In, First Out"
        LIFO = "LIFO", "Last In, First Out"
        AVERAGE = "AVERAGE", "Weighted Average"
        SPECIFIC = "SPECIFIC", "Specific Identification"

    costing_method = models.CharField(
        max_length=20, choices=CostingMethod.choices, default=CostingMethod.FIFO
    )
    include_waste_in_cost = models.BooleanField(default=True)

    # Alerts
    low_stock_alert_enabled = models.BooleanField(default=True)
    expiry_alert_enabled = models.BooleanField(default=True)
    expiry_alert_days = models.PositiveIntegerField(default=7)
    negative_stock_alert = models.BooleanField(default=True)

    # Defaults
    default_location = models.ForeignKey(
        'stock.StockLocation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    default_production_location = models.ForeignKey(
        'stock.StockLocation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    default_receiving_location = models.ForeignKey(
        'stock.StockLocation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Approvals
    require_po_approval = models.BooleanField(default=False)
    require_transfer_approval = models.BooleanField(default=False)
    require_adjustment_approval = models.BooleanField(default=False)
    require_count_approval = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        verbose_name = "stock settings"
        verbose_name_plural = "stock settings"

    def save(self, *args, **kwargs):
        # Enforce singleton: always use pk=1
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['default_location_uuid'] = str(self.default_location.uuid) if self.default_location else None
        data['default_production_location_uuid'] = str(self.default_production_location.uuid) if self.default_production_location else None
        data['default_receiving_location_uuid'] = str(self.default_receiving_location.uuid) if self.default_receiving_location else None
        return data

    def __str__(self):
        return "Stock Settings"


class StockAlertConfig(SyncMixin, models.Model):
    class AlertType(models.TextChoices):
        LOW_STOCK = "LOW_STOCK", "Low Stock"
        EXPIRING = "EXPIRING", "Expiring"
        NEGATIVE = "NEGATIVE", "Negative Stock"
        OVERSTOCK = "OVERSTOCK", "Overstock"


    alert_type = models.CharField(max_length=20, choices=AlertType.choices)
    notify_email = models.BooleanField(default=False)
    notify_telegram = models.BooleanField(default=True)
    notify_in_app = models.BooleanField(default=True)
    threshold_value = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    def __str__(self):
        return f"Alert: {self.get_alert_type_display()}"
