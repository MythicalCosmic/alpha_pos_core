"""Suppliers models for the stock app.

Auto-extracted from the original monolithic stock/models.py (smart_pos T5
refactor). Cross-model FKs are still expressed as direct class references
where the referenced model lives in this same submodule; FKs that cross
submodules are expressed as string refs like `'stock.StockUnit'` to avoid
import-order coupling.
"""
from django.db import models

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
        RETURN = 'RETURN', 'Return (debt -)'
        ADJUSTMENT = 'ADJUSTMENT', 'Adjustment'

    class SourceAccount(models.TextChoices):
        SAFE = 'SAFE', 'Safe'
        BANK = 'BANK', 'Bank'
        DRAWER = 'DRAWER', 'Shift drawer'

    supplier = models.ForeignKey(
        Supplier, on_delete=models.CASCADE, related_name='ledger',
    )
    type = models.CharField(max_length=12, choices=Type.choices)
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

    class Meta:
        ordering = ['-created_at']

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['supplier_uuid'] = str(self.supplier.uuid) if self.supplier else None
        data['performed_by_uuid'] = str(self.performed_by.uuid) if self.performed_by else None
        return data

    def __str__(self):
        return f"{self.supplier_id}:{self.type} {self.amount}"


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
