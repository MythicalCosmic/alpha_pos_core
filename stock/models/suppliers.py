"""Supplier models."""
import uuid

from django.db import models
from django.utils import timezone

from base.models import SyncMixin, SyncManager

class Supplier(SyncMixin, models.Model):

    code = models.CharField(max_length=20, unique=True, blank=True, null=True)
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True, default="")
    contact_person = models.CharField(max_length=100, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=50, blank=True, default="")
    mobile = models.CharField(max_length=50, blank=True, default="")
    address = models.TextField(blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    country = models.CharField(max_length=100, blank=True, default="")
    tax_id = models.CharField(max_length=50, blank=True, default="")

    payment_terms_days = models.PositiveIntegerField(default=30)
    credit_limit = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    current_balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=0
    )
    currency = models.CharField(max_length=3, default="UZS")
    lead_time_days = models.PositiveIntegerField(default=1)
    minimum_order_value = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="1 to 5"
    )

    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # current_balance is computed from the SupplierTransaction ledger on the
    # branch; a pulled catalog copy (name/terms edited on the cloud) must not
    # reset it. Refused on branch ingest, accepted by the cloud aggregator.
    SYNC_WRITE_DENYLIST = frozenset({'current_balance'})

    objects = SyncManager()

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=['branch_id', 'currency', 'current_balance'])]

    def __str__(self):
        return self.name


class SupplierTransaction(SyncMixin, models.Model):
    """Append-only supplier ledger. Positive balance = we owe the supplier.

    Mirrors the treasury ledger (balance_before/after so the books reconcile).
    `source_account` (for payments) is a plain enum, NOT an FK to the per-branch
    TreasuryAccount, so this ledger syncs coherently.
    """
    class Type(models.TextChoices):
        PURCHASE = 'PURCHASE', 'Purchase (debt +)'
        PAYMENT = 'PAYMENT', 'Payment (debt -)'
        PAYMENT_REVERSAL = 'PAYMENT_REVERSAL', 'Payment reversal (debt +)'
        RETURN = 'RETURN', 'Return (debt -)'
        ADJUSTMENT = 'ADJUSTMENT', 'Adjustment'

    class SourceAccount(models.TextChoices):
        SAFE = 'SAFE', 'Safe'
        BANK = 'BANK', 'Bank'
        DRAWER = 'DRAWER', 'Shift drawer'

    supplier = models.ForeignKey(
        Supplier, on_delete=models.CASCADE, related_name='ledger',
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    # Always stored positive; the sign applied to the balance comes from `type`.
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    balance_before = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    balance_after = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    source_account = models.CharField(
        max_length=10, choices=SourceAccount.choices, blank=True, default='',
    )
    fee = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    note = models.TextField(blank=True, default='')
    reference_type = models.CharField(max_length=50, blank=True, default='')
    reference_id = models.PositiveIntegerField(null=True, blank=True)
    performed_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='supplier_transactions',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Branch-owned money figures.
    SYNC_WRITE_DENYLIST = frozenset({
        'amount', 'balance_before', 'balance_after', 'fee',
    })

    objects = SyncManager()
    _sync_append_only = True

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['supplier', 'type', 'created_at']),
            models.Index(fields=['branch_id', 'reference_type', 'reference_id']),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['supplier_uuid'] = str(self.supplier.uuid) if self.supplier else None
        data['performed_by_uuid'] = str(self.performed_by.uuid) if self.performed_by else None
        return data

    def __str__(self):
        return f"{self.supplier_id}:{self.type} {self.amount}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise TypeError('SupplierTransaction is append-only and cannot be updated')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError('SupplierTransaction is append-only and cannot be deleted')

    def hard_delete(self, *args, **kwargs):
        raise TypeError('SupplierTransaction is append-only and cannot be deleted')


class SupplierPayment(models.Model):
    class SourceAccount(models.TextChoices):
        SAFE = 'SAFE', 'Safe'
        BANK = 'BANK', 'Bank'

    class AllocationMode(models.TextChoices):
        EXPLICIT = 'EXPLICIT', 'Explicit'
        AUTO_OLDEST_DUE = 'AUTO_OLDEST_DUE', 'Auto oldest due'
        LEGACY_UNFUNDED = 'LEGACY_UNFUNDED', 'Legacy unfunded'

    class Status(models.TextChoices):
        POSTED = 'POSTED', 'Posted'
        LEGACY_UNFUNDED = 'LEGACY_UNFUNDED', 'Legacy unfunded'
        REVERSED = 'REVERSED', 'Reversed'

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    branch_id = models.CharField(max_length=50, db_index=True)
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name='payments',
    )
    principal_uzs = models.DecimalField(max_digits=15, decimal_places=2)
    fee_uzs = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_debited_uzs = models.DecimalField(max_digits=15, decimal_places=2)
    source_account = models.CharField(
        max_length=10, choices=SourceAccount.choices, blank=True, default='',
    )
    allocation_mode = models.CharField(
        max_length=20, choices=AllocationMode.choices,
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    supplier_balance_before_uzs = models.DecimalField(max_digits=15, decimal_places=2)
    supplier_balance_after_uzs = models.DecimalField(max_digits=15, decimal_places=2)
    source_balance_before_uzs = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
    )
    source_balance_after_uzs = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
    )
    treasury_transaction = models.OneToOneField(
        'base.TreasuryTransaction', on_delete=models.PROTECT,
        null=True, blank=True, related_name='supplier_payment',
    )
    supplier_transaction = models.OneToOneField(
        SupplierTransaction, on_delete=models.PROTECT,
        related_name='supplier_payment',
    )
    payment_action_id = models.UUIDField(null=True, blank=True, unique=True)
    idempotency_key = models.CharField(max_length=128, blank=True, default='')
    request_hash = models.CharField(max_length=64, blank=True, default='')
    note = models.TextField(blank=True, default='')
    performed_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='supplier_payments',
    )
    actor_display_snapshot = models.CharField(max_length=200, blank=True, default='')
    paid_at = models.DateTimeField(default=timezone.now)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reversed_supplier_payments',
    )
    reversal_reason = models.TextField(blank=True, default='')
    reversal_action_id = models.UUIDField(null=True, blank=True, unique=True)
    reversal_idempotency_key = models.CharField(
        max_length=128, blank=True, default='',
    )
    reversed_actor_display_snapshot = models.CharField(
        max_length=200, blank=True, default='',
    )
    treasury_reversal = models.OneToOneField(
        'base.TreasuryTransaction', on_delete=models.PROTECT,
        null=True, blank=True, related_name='supplier_payment_reversal',
    )
    supplier_reversal = models.OneToOneField(
        SupplierTransaction, on_delete=models.PROTECT,
        null=True, blank=True, related_name='supplier_payment_reversal',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-paid_at', '-id']
        indexes = [
            models.Index(fields=['branch_id', 'supplier', 'paid_at']),
            models.Index(fields=['status', 'paid_at']),
        ]


class SupplierPaymentAllocation(models.Model):
    payment = models.ForeignKey(
        SupplierPayment, on_delete=models.PROTECT, related_name='allocations',
    )
    purchase_order = models.ForeignKey(
        'stock.PurchaseOrder', on_delete=models.PROTECT,
        related_name='payment_allocations',
    )
    amount_uzs = models.DecimalField(max_digits=15, decimal_places=2)
    payment_status_snapshot = models.CharField(max_length=20)
    remaining_uzs_snapshot = models.DecimalField(max_digits=15, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['purchase_order_id']
        constraints = [
            models.UniqueConstraint(
                fields=['payment', 'purchase_order'],
                name='uniq_supplier_payment_purchase_order',
            ),
        ]


class SupplierStockItem(SyncMixin, models.Model):

    supplier = models.ForeignKey(
        Supplier, on_delete=models.CASCADE, related_name="stock_items"
    )
    stock_item = models.ForeignKey(
        'stock.StockItem', on_delete=models.CASCADE, related_name="suppliers"
    )
    supplier_sku = models.CharField(max_length=50, blank=True, default="")
    supplier_name = models.CharField(
        max_length=200, blank=True, default="",
        help_text="What the supplier calls this item",
    )
    unit = models.ForeignKey('stock.StockUnit', on_delete=models.PROTECT, related_name="+")
    price = models.DecimalField(max_digits=15, decimal_places=4)
    currency = models.CharField(max_length=3, default="UZS")
    min_order_qty = models.DecimalField(
        max_digits=15, decimal_places=4, default=1
    )
    pack_size = models.DecimalField(max_digits=15, decimal_places=4, default=1)
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)
    is_preferred = models.BooleanField(default=False)
    last_price_update = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        unique_together = [("supplier", "stock_item")]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['supplier_uuid'] = str(self.supplier.uuid) if self.supplier else None
        data['stock_item_uuid'] = str(self.stock_item.uuid) if self.stock_item else None
        data['unit_uuid'] = str(self.unit.uuid) if self.unit else None
        return data

    def __str__(self):
        return f"{self.supplier.name} → {self.stock_item.name}"
