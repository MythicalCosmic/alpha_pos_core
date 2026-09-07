"""Purchase order and receiving models."""
from django.db import models

from base.models import SyncMixin, SyncManager

class PurchaseDocumentType(models.TextChoices):
    PURCHASE_ORDER = 'PURCHASE_ORDER', 'Planned purchase order'
    DIRECT_INVOICE = 'DIRECT_INVOICE', 'Direct supplier invoice'

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


    source_type = models.CharField(max_length=20, choices=PurchaseDocumentType.choices,
                                   default=PurchaseDocumentType.PURCHASE_ORDER, db_index=True)
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
        indexes = [
            models.Index(
                fields=['branch_id', 'supplier', 'payment_due_date', 'payment_status'],
            ),
        ]

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
    quantity_canceled = models.DecimalField(
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


    source_type = models.CharField(max_length=20, choices=PurchaseDocumentType.choices,
                                   default=PurchaseDocumentType.PURCHASE_ORDER, db_index=True)
    supplier = models.ForeignKey('stock.Supplier', on_delete=models.PROTECT,
                                 null=True, blank=True, related_name='invoices')
    supplier_invoice_number = models.CharField(max_length=100, blank=True, default='')
    supplier_invoice_number_normalized = models.CharField(max_length=200, blank=True, default='')
    invoice_date = models.DateField(null=True, blank=True, db_index=True)
    posting_manifest = models.JSONField(default=dict, blank=True)
    posted_by = models.ForeignKey('base.User', on_delete=models.PROTECT, null=True,
                                  blank=True, related_name='posted_supplier_invoices')
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey('base.User', on_delete=models.PROTECT, null=True,
                                    blank=True, related_name='reversed_supplier_invoices')
    reversal_reason = models.CharField(max_length=1000, blank=True, default='')
    reversal_manifest = models.JSONField(default=dict, blank=True)
    replaces = models.ForeignKey('self', on_delete=models.PROTECT, null=True,
                                 blank=True, related_name='replacements')
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
    completed_at = models.DateTimeField(null=True, blank=True)
    supplier_balance_before = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
    )
    supplier_balance_after = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
    )
    received_value_uzs = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
    )
    supplier_transaction = models.OneToOneField(
        'stock.SupplierTransaction', on_delete=models.PROTECT,
        null=True, blank=True, related_name='purchase_receiving',
    )
    completion_action_id = models.UUIDField(null=True, blank=True, unique=True)
    completion_idempotency_key = models.CharField(
        max_length=128, blank=True, default='',
    )
    quality_posting_policy = models.CharField(
        max_length=64, blank=True, default='',
    )
    over_receipt_approved_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, null=True, blank=True,
        related_name='approved_purchase_over_receipts',
    )
    over_receipt_approved_at = models.DateTimeField(null=True, blank=True)
    over_receipt_reason = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['branch_id', 'supplier', 'supplier_invoice_number_normalized'],
            condition=models.Q(source_type='DIRECT_INVOICE') & ~models.Q(supplier_invoice_number_normalized=''),
            name='uniq_direct_supplier_invoice_number',
        )]
        indexes = [models.Index(fields=['branch_id', 'source_type', 'invoice_date'])]

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk, source_type='DIRECT_INVOICE').first()
            if old is not None and old.posting_manifest:
                fixed = ('uuid', 'created_at', 'quality_posting_policy',
                         'over_receipt_approved_by_id', 'over_receipt_approved_at', 'over_receipt_reason',
                         'source_type', 'supplier_id', 'location_id', 'purchase_order_id',
                         'supplier_invoice_number', 'supplier_invoice_number_normalized',
                         'invoice_date', 'received_date', 'receiving_number', 'status',
                         'received_by_id', 'posted_by_id', 'completed_at', 'notes',
                         'received_value_uzs', 'supplier_balance_before', 'supplier_balance_after',
                         'supplier_transaction_id', 'completion_action_id', 'completion_idempotency_key',
                         'posting_manifest', 'replaces_id', 'is_deleted', 'branch_id')
                if any(getattr(self, name) != getattr(old, name) for name in fixed):
                    raise TypeError('Posted supplier invoice is immutable')
                if old.reversed_at and any(getattr(self, name) != getattr(old, name) for name in (
                    'reversed_at', 'reversed_by_id', 'reversal_reason', 'reversal_manifest',
                )):
                    raise TypeError('Supplier invoice reversal is immutable')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._has_posted_direct_invoice():
            raise TypeError('Posted supplier invoice cannot be deleted')
        return super().delete(*args, **kwargs)

    def hard_delete(self, *args, **kwargs):
        if self._has_posted_direct_invoice():
            raise TypeError('Posted supplier invoice cannot be deleted')
        return super().hard_delete(*args, **kwargs)

    def _has_posted_direct_invoice(self):
        return type(self).objects.filter(pk=self.pk, source_type='DIRECT_INVOICE').exclude(posting_manifest={}).exists()

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['purchase_order_uuid'] = str(self.purchase_order.uuid) if self.purchase_order else None
        data['location_uuid'] = str(self.location.uuid) if self.location else None
        data['received_by_uuid'] = str(self.received_by.uuid) if self.received_by else None
        data['supplier_uuid'] = str(self.supplier.uuid) if self.supplier else None
        data['posted_by_uuid'] = str(self.posted_by.uuid) if self.posted_by else None
        data['reversed_by_uuid'] = str(self.reversed_by.uuid) if self.reversed_by else None
        data['replaces_invoice_uuid'] = str(self.replaces.uuid) if self.replaces else None
        data['over_receipt_approved_by_uuid'] = (
            str(self.over_receipt_approved_by.uuid)
            if self.over_receipt_approved_by else None
        )
        data['supplier_transaction_uuid'] = (
            str(self.supplier_transaction.uuid)
            if self.supplier_transaction else None
        )
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
    supplier_stock_item = models.ForeignKey('stock.SupplierStockItem', on_delete=models.PROTECT,
                                            null=True, blank=True, related_name='invoice_lines')
    line_total_uzs = models.DecimalField(max_digits=15, decimal_places=0, null=True, blank=True)
    is_free = models.BooleanField(default=False)
    free_reason = models.CharField(max_length=1000, blank=True, default='')
    invoice_snapshot = models.JSONField(default=dict, blank=True)
    stock_transaction = models.OneToOneField('stock.StockTransaction', on_delete=models.PROTECT,
                                             null=True, blank=True, related_name='invoice_line')
    quantity_received = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    conversion_to_base_snapshot = models.DecimalField(
        max_digits=15, decimal_places=6, null=True, blank=True,
    )
    base_quantity = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True,
    )
    base_unit = models.ForeignKey(
        'stock.StockUnit', on_delete=models.PROTECT,
        null=True, blank=True, related_name='+',
    )
    base_unit_cost = models.DecimalField(
        max_digits=15, decimal_places=4, null=True, blank=True,
    )
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

    def _posted_direct_invoice(self):
        original = type(self).objects.filter(pk=self.pk).values_list('receiving_id', flat=True).first() if self.pk else None
        return PurchaseReceiving.objects.filter(
            pk__in=[self.receiving_id, original], source_type='DIRECT_INVOICE', status='COMPLETED',
        ).exists()

    def save(self, *args, **kwargs):
        if self._posted_direct_invoice():
            from base.services.sync.context import is_authoritative_cloud_pull
            if not (self._state.adding and kwargs.get('_syncing') and is_authoritative_cloud_pull()):
                raise TypeError('Posted supplier invoice line is immutable')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._posted_direct_invoice():
            raise TypeError('Posted supplier invoice line cannot be deleted')
        return super().delete(*args, **kwargs)

    def hard_delete(self, *args, **kwargs):
        if self._posted_direct_invoice():
            raise TypeError('Posted supplier invoice line cannot be deleted')
        return super().hard_delete(*args, **kwargs)

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['receiving_uuid'] = str(self.receiving.uuid) if self.receiving else None
        data['po_item_uuid'] = str(self.po_item.uuid) if self.po_item else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        data['base_unit_uuid'] = str(self.base_unit.uuid) if self.base_unit else None
        data['batch_created_uuid'] = str(self.batch_created.uuid) if self.batch_created else None
        data['supplier_stock_item_uuid'] = str(self.supplier_stock_item.uuid) if self.supplier_stock_item else None
        data['stock_transaction_uuid'] = str(self.stock_transaction.uuid) if self.stock_transaction else None
        return data

    def __str__(self):
        return f"{self.stock_item.name} × {self.quantity_received}"


class PurchaseReceivingCorrection(SyncMixin, models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    receiving = models.ForeignKey(
        PurchaseReceiving, on_delete=models.PROTECT, related_name='corrections',
    )
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, related_name='requested_receiving_corrections',
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        'base.User', on_delete=models.PROTECT, null=True, blank=True,
        related_name='reviewed_receiving_corrections',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    supplier_balance_before = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    supplier_balance_after = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)

    objects = SyncManager()

    class Meta:
        ordering = ['-requested_at']
        constraints = [
            models.UniqueConstraint(
                fields=['receiving'], condition=models.Q(status='PENDING'),
                name='uniq_pending_receiving_correction',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['receiving_uuid'] = str(self.receiving.uuid) if self.receiving else None
        data['requested_by_uuid'] = str(self.requested_by.uuid) if self.requested_by else None
        data['reviewed_by_uuid'] = str(self.reviewed_by.uuid) if self.reviewed_by else None
        return data
