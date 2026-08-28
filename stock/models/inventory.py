"""Inventory level, batch, and transaction models."""
from django.db import models

from base.models import SyncMixin, SyncManager

class StockLevel(SyncMixin, models.Model):
    """
    Denormalized current stock level per item per location.
    Updated by stock transactions.
    """


    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.CASCADE, related_name="stock_levels"
    )
    location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.CASCADE, related_name="stock_levels"
    )
    quantity = models.DecimalField(max_digits=15, decimal_places=4, default=0)
    reserved_quantity = models.DecimalField(
        max_digits=15, decimal_places=4, default=0
    )
    pending_in_quantity = models.DecimalField(
        max_digits=15, decimal_places=4, default=0
    )
    pending_out_quantity = models.DecimalField(
        max_digits=15, decimal_places=4, default=0
    )
    last_counted_at = models.DateTimeField(null=True, blank=True)
    last_restocked_at = models.DateTimeField(null=True, blank=True)
    last_movement_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        unique_together = [("stock_item", "location")]

    @property
    def available_quantity(self):
        return self.quantity - self.reserved_quantity

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['location_uuid'] = str(self.location.uuid) if self.location else None
        return data

    def __str__(self):
        return f"{self.stock_item.name} @ {self.location.name}: {self.quantity}"


class StockAdjustmentRequest(SyncMixin, models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        CANCELLED = 'CANCELLED', 'Cancelled'

    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name='adjustment_requests',
    )
    location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.PROTECT, related_name='adjustment_requests',
    )
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name='+')
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    reason = models.TextField()
    evidence = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, related_name='stock_adjustment_requests',
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, null=True, blank=True,
        related_name='reviewed_stock_adjustment_requests',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    stock_transaction = models.OneToOneField(
        'stock.StockTransaction', on_delete=models.PROTECT, null=True, blank=True,
        related_name='adjustment_request',
    )

    objects = SyncManager()

    class Meta:
        ordering = ['-requested_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['location_uuid'] = str(self.location.uuid) if self.location else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        data['requested_by_uuid'] = str(self.requested_by.uuid) if self.requested_by else None
        data['reviewed_by_uuid'] = str(self.reviewed_by.uuid) if self.reviewed_by else None
        data['stock_transaction_uuid'] = (
            str(self.stock_transaction.uuid) if self.stock_transaction else None
        )
        return data


class StockBatch(SyncMixin, models.Model):
    class BatchStatus(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        RESERVED = "RESERVED", "Reserved"
        QUARANTINE = "QUARANTINE", "Quarantine"
        EXPIRED = "EXPIRED", "Expired"
        CONSUMED = "CONSUMED", "Consumed"


    batch_number = models.CharField(max_length=100)
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.CASCADE, related_name="batches"
    )
    location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.PROTECT, related_name="batches"
    )
    initial_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    current_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    reserved_quantity = models.DecimalField(
        max_digits=15, decimal_places=4, default=0
    )
    unit_cost = models.DecimalField(max_digits=15, decimal_places=4, default=0)
    total_cost = models.DecimalField(max_digits=15, decimal_places=4, default=0)

    manufactured_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)

    supplier = models.ForeignKey(
        'stock.Supplier',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="batches",
    )
    purchase_order = models.ForeignKey(
        'stock.PurchaseOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="batches",
    )
    production_order = models.ForeignKey(
        "ProductionOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="output_batches",
    )

    status = models.CharField(
        max_length=20, choices=BatchStatus.choices, default=BatchStatus.AVAILABLE
    )
    quality_status = models.CharField(max_length=20, default="PASSED")
    notes = models.TextField(blank=True, default="")
    received_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        unique_together = [("batch_number", "stock_item")]
        verbose_name_plural = "stock batches"

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['location_uuid'] = str(self.location.uuid) if self.location else None
        data['supplier_uuid'] = str(self.supplier.uuid) if self.supplier else None
        data['purchase_order_uuid'] = str(self.purchase_order.uuid) if self.purchase_order else None
        data['production_order_uuid'] = str(self.production_order.uuid) if self.production_order else None
        return data

    def __str__(self):
        return f"Batch {self.batch_number} – {self.stock_item.name}"


class StockTransaction(SyncMixin, models.Model):
    class MovementType(models.TextChoices):
        PURCHASE_IN = "PURCHASE_IN", "Purchase In"
        SALE_OUT = "SALE_OUT", "Sale Out"
        TRANSFER_IN = "TRANSFER_IN", "Transfer In"
        TRANSFER_OUT = "TRANSFER_OUT", "Transfer Out"
        PRODUCTION_IN = "PRODUCTION_IN", "Production In"
        PRODUCTION_OUT = "PRODUCTION_OUT", "Production Out"
        ADJUSTMENT_PLUS = "ADJUSTMENT_PLUS", "Adjustment +"
        ADJUSTMENT_MINUS = "ADJUSTMENT_MINUS", "Adjustment −"
        WASTE = "WASTE", "Waste"
        SPOILAGE = "SPOILAGE", "Spoilage"
        RETURN_FROM_CUSTOMER = "RETURN_FROM_CUSTOMER", "Return from Customer"
        RETURN_TO_SUPPLIER = "RETURN_TO_SUPPLIER", "Return to Supplier"
        COUNT_ADJUSTMENT = "COUNT_ADJUSTMENT", "Count Adjustment"
        OPENING_BALANCE = "OPENING_BALANCE", "Opening Balance"
        RESERVATION = "RESERVATION", "Reservation"
        RESERVATION_RELEASE = "RESERVATION_RELEASE", "Reservation Release"


    transaction_number = models.CharField(max_length=50, unique=True)
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="transactions"
    )
    location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.PROTECT, related_name="transactions"
    )
    batch = models.ForeignKey(
        StockBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
    )
    movement_type = models.CharField(
        max_length=30, choices=MovementType.choices, db_index=True
    )

    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    base_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    quantity_before = models.DecimalField(max_digits=15, decimal_places=4)
    quantity_after = models.DecimalField(max_digits=15, decimal_places=4)
    unit_cost = models.DecimalField(max_digits=15, decimal_places=4, default=0)
    total_cost = models.DecimalField(max_digits=15, decimal_places=4, default=0)

    # Generic reference to source document
    reference_type = models.CharField(max_length=50, blank=True, default="")
    reference_id = models.PositiveIntegerField(null=True, blank=True)

    # Explicit FKs for the most common reference types
    order = models.ForeignKey(
        "base.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_transactions",
    )
    order_item = models.ForeignKey(
        "base.OrderItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_transactions",
        help_text="Exact sold line that caused this movement, when available.",
    )
    production_order = models.ForeignKey(
        "ProductionOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_transactions",
    )
    transfer = models.ForeignKey(
        "StockTransfer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_transactions",
    )

    user = models.ForeignKey(
        'base.User',
        on_delete=models.PROTECT,
        related_name="stock_transactions",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = SyncManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["stock_item", "created_at"]),
            models.Index(fields=["movement_type", "created_at"]),
            models.Index(fields=["reference_type", "reference_id"]),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['location_uuid'] = str(self.location.uuid) if self.location else None
        data['batch_uuid'] = str(self.batch.uuid) if self.batch else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        data['order_uuid'] = str(self.order.uuid) if self.order else None
        data['order_item_uuid'] = (
            str(self.order_item.uuid) if self.order_item else None
        )
        data['production_order_uuid'] = str(self.production_order.uuid) if self.production_order else None
        data['transfer_uuid'] = str(self.transfer.uuid) if self.transfer else None
        data['user_uuid'] = str(self.user.uuid) if self.user else None
        return data

    def __str__(self):
        return f"{self.transaction_number} | {self.get_movement_type_display()}"
