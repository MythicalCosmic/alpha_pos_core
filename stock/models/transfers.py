"""Stock transfers models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class StockTransfer(SyncMixin, models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        REQUESTED = "REQUESTED", "Requested"
        APPROVED = "APPROVED", "Approved"
        IN_TRANSIT = "IN_TRANSIT", "In Transit"
        RECEIVED = "RECEIVED", "Received"
        CANCELED = "CANCELED", "Canceled"

    class TransferType(models.TextChoices):
        INTERNAL = "INTERNAL", "Internal"
        BRANCH_TO_BRANCH = "BRANCH_TO_BRANCH", "Branch to Branch"


    transfer_number = models.CharField(max_length=50, unique=True)
    from_location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.PROTECT, related_name="transfers_out"
    )
    to_location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.PROTECT, related_name="transfers_in"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    transfer_type = models.CharField(
        max_length=20,
        choices=TransferType.choices,
        default=TransferType.INTERNAL,
    )

    requested_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_transfers",
    )
    approved_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_transfers",
    )
    shipped_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shipped_transfers",
    )
    received_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_transfers",
    )

    requested_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ["-created_at"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['from_location_uuid'] = str(self.from_location.uuid) if self.from_location else None
        data['to_location_uuid'] = str(self.to_location.uuid) if self.to_location else None
        data['requested_by_uuid'] = str(self.requested_by.uuid) if self.requested_by else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        data['shipped_by_uuid'] = str(self.shipped_by.uuid) if self.shipped_by else None
        data['received_by_uuid'] = str(self.received_by.uuid) if self.received_by else None
        return data

    def __str__(self):
        return f"TRF-{self.transfer_number}"


class StockTransferItem(SyncMixin, models.Model):

    transfer = models.ForeignKey(
        StockTransfer, on_delete=models.CASCADE, related_name="items"
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
    requested_qty = models.DecimalField(max_digits=15, decimal_places=4)
    approved_qty = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    shipped_qty = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    received_qty = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True
    )
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    variance_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['transfer_uuid'] = str(self.transfer.uuid) if self.transfer else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['batch_uuid'] = str(self.batch.uuid) if self.batch else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return f"{self.stock_item.name} × {self.requested_qty}"
