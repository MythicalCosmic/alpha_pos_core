"""Stock counts + variance reasons models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class VarianceReasonCode(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    requires_approval = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def __str__(self):
        return f"{self.code}: {self.name}"


class StockCount(SyncMixin, models.Model):
    class CountType(models.TextChoices):
        FULL = "FULL", "Full Count"
        PARTIAL = "PARTIAL", "Partial Count"
        CYCLE = "CYCLE", "Cycle Count"
        SPOT = "SPOT", "Spot Check"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
        APPROVED = "APPROVED", "Approved"
        CANCELED = "CANCELED", "Canceled"


    count_number = models.CharField(max_length=50, unique=True)
    location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.PROTECT, related_name="stock_counts"
    )
    count_type = models.CharField(max_length=20, choices=CountType.choices)
    category_filter = models.ForeignKey(
        'stock.StockCategory',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="If set, only items in this category will be counted",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    counted_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_counts",
    )
    approved_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_stock_counts",
    )
    auto_adjust = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ["-created_at"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['location_uuid'] = str(self.location.uuid) if self.location else None
        data['category_filter_uuid'] = str(self.category_filter.uuid) if self.category_filter else None
        data['counted_by_uuid'] = str(self.counted_by.uuid) if self.counted_by else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        return data

    def __str__(self):
        return f"CNT-{self.count_number}"


class StockCountItem(SyncMixin, models.Model):

    stock_count = models.ForeignKey(
        StockCount, on_delete=models.CASCADE, related_name="items"
    )
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="+"
    )
    batch = models.ForeignKey(
        'stock.StockBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    system_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    counted_quantity = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    variance = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    variance_percentage = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True
    )
    variance_cost = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    reason_code = models.ForeignKey(
        VarianceReasonCode,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    notes = models.TextField(blank=True, default="")
    is_adjusted = models.BooleanField(default=False)
    adjustment_transaction = models.ForeignKey(
        'stock.StockTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['stock_count_uuid'] = str(self.stock_count.uuid) if self.stock_count else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['batch_uuid'] = str(self.batch.uuid) if self.batch else None
        data['reason_code_uuid'] = str(self.reason_code.uuid) if self.reason_code else None
        data['adjustment_transaction_uuid'] = str(self.adjustment_transaction.uuid) if self.adjustment_transaction else None
        return data

    def __str__(self):
        return f"{self.stock_item.name}: system={self.system_quantity}, counted={self.counted_quantity}"
