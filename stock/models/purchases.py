"""Purchases (POs + receivings) models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

from base.models import SyncMixin, SyncManager

class PurchaseOrder(SyncMixin, models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SENT = "SENT", "Sent"
        CONFIRMED = "CONFIRMED", "Confirmed"
        PARTIAL = "PARTIAL", "Partially Received"
        RECEIVED = "RECEIVED", "Received"
        CANCELED = "CANCELED", "Canceled"

    class PaymentStatus(models.TextChoices):
        UNPAID = "UNPAID", "Unpaid"
        PARTIAL = "PARTIAL", "Partial"
        PAID = "PAID", "Paid"


    order_number = models.CharField(max_length=50, unique=True)
    supplier = models.ForeignKey(
        'stock.Supplier', on_delete=models.PROTECT, related_name="purchase_orders"
    )
    delivery_location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.PROTECT, related_name="purchase_orders"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    order_date = models.DateField()
    expected_date = models.DateTimeField(null=True, blank=True)
    received_date = models.DateField(null=True, blank=True)

    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    shipping_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="UZS")

    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID
    )
    amount_paid = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    payment_due_date = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        'base.User',
        on_delete=models.PROTECT,
        related_name="created_purchase_orders",
    )
    approved_by = models.ForeignKey(
        'base.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_purchase_orders",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ["-order_date"]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['supplier_uuid'] = str(self.supplier.uuid) if self.supplier else None
        data['delivery_location_uuid'] = str(self.delivery_location.uuid) if self.delivery_location else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        data['approved_by_uuid'] = str(self.approved_by.uuid) if self.approved_by else None
        return data

    def __str__(self):
        return f"PO-{self.order_number}"


class PurchaseOrderItem(SyncMixin, models.Model):

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="items"
    )
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="+"
    )
    supplier_stock_item = models.ForeignKey(
        'stock.SupplierStockItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    quantity_ordered = models.DecimalField(max_digits=15, decimal_places=4)
    quantity_received = models.DecimalField(
        max_digits=15, decimal_places=4, default=0
    )
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    unit_price = models.DecimalField(max_digits=15, decimal_places=4)
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0
    )
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    total_price = models.DecimalField(max_digits=15, decimal_places=4)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['purchase_order_uuid'] = str(self.purchase_order.uuid) if self.purchase_order else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['supplier_stock_item_uuid'] = str(self.supplier_stock_item.uuid) if self.supplier_stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return f"{self.stock_item.name} × {self.quantity_ordered}"


class PurchaseReceiving(SyncMixin, models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        COMPLETED = "COMPLETED", "Completed"


    receiving_number = models.CharField(max_length=50, unique=True)
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, related_name="receivings"
    )
    location = models.ForeignKey(
        'stock.StockLocation', on_delete=models.PROTECT, related_name="+"
    )
    received_date = models.DateField()
    received_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['purchase_order_uuid'] = str(self.purchase_order.uuid) if self.purchase_order else None
        data['location_uuid'] = str(self.location.uuid) if self.location else None
        data['received_by_uuid'] = str(self.received_by.uuid) if self.received_by else None
        return data

    def __str__(self):
        return f"RCV-{self.receiving_number}"


class PurchaseReceivingItem(SyncMixin, models.Model):
    class QualityStatus(models.TextChoices):
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"
        PENDING = "PENDING", "Pending"


    receiving = models.ForeignKey(
        PurchaseReceiving, on_delete=models.CASCADE, related_name="items"
    )
    po_item = models.ForeignKey(
        PurchaseOrderItem, on_delete=models.PROTECT, related_name="receiving_items"
    )
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.PROTECT, related_name="+"
    )
    quantity_received = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    batch_number = models.CharField(max_length=100, blank=True, default="")
    expiry_date = models.DateField(null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=15, decimal_places=4)
    quality_status = models.CharField(
        max_length=20,
        choices=QualityStatus.choices,
        default=QualityStatus.PASSED,
    )
    notes = models.TextField(blank=True, default="")
    # Set after batch is created during receiving
    batch_created = models.ForeignKey(
        "StockBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SyncManager()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['receiving_uuid'] = str(self.receiving.uuid) if self.receiving else None
        data['po_item_uuid'] = str(self.po_item.uuid) if self.po_item else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        data['batch_created_uuid'] = str(self.batch_created.uuid) if self.batch_created else None
        return data

    def __str__(self):
        return f"{self.stock_item.name} × {self.quantity_received}"
