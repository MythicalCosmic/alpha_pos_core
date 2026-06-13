"""Per-shift cashbox + shift settlement models.

Design (see the Money & Shift Logic spec):
  * The cashier's live cash is a PER-SHIFT drawer, not the old global
    CashRegister. It's *derived* (cash sales - cash expenses - cash returns
    since shift start), so there is no stored running-balance column to drift.
  * At shift close the cashier counts each tender type; ShiftPaymentTotal
    freezes expected/counted/difference per method, then the manager confirms
    and the money posts to the branch SAFE (cash) / BANK (cards).
  * CashboxExpense is money paid OUT of the drawer (its own model, not hr.Expense).

Money fields carry SYNC_WRITE_DENYLIST so a branch only ever owns its own
drawer figures — a pulled peer/cloud copy can't rewrite them (the cloud, the
trusted aggregator, still accepts them). Mirrors base.Order.
"""
from django.db import models

from base.models import SyncMixin, SyncManager


# Concrete tender types a drawer is counted in. MIXED is never stored here —
# a mixed-tender order is split into its component OrderPayment rows, each of
# which lands under its own method below. Kept loose (CharField, no DB choices)
# because PaymentMethodConfig is operator-editable.
PAYMENT_METHODS = ('CASH', 'UZCARD', 'HUMO', 'PAYME')


class ShiftPaymentTotal(SyncMixin, models.Model):
    """Per-(shift, method) settlement row.

    expected  = system figure (Σ OrderPayment for the method in the shift window,
                minus cash expenses for CASH).
    counted   = what the cashier physically counted at close (blind).
    confirmed = the manager's final accepted figure (posts to SAFE/BANK).
    difference= counted - expected (frozen at close).
    """
    shift = models.ForeignKey(
        'base.Shift', on_delete=models.CASCADE, related_name='payment_totals',
    )
    method = models.CharField(max_length=10)
    expected_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    counted_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    confirmed_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    difference = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Branch-owned money — never let a pulled peer copy overwrite a till's
    # own settlement figures (the cloud aggregator still accepts them).
    SYNC_WRITE_DENYLIST = frozenset({
        'expected_amount', 'counted_amount', 'confirmed_amount', 'difference',
    })

    # (shift, method) is the real identity: reconcile an incoming row onto the
    # existing one for the same shift+tender instead of INSERTing a duplicate
    # that trips uniq_shift_method_active. 'shift' is resolved from shift_uuid.
    SYNC_NATURAL_KEYS = ('shift', 'method')

    objects = SyncManager()

    class Meta:
        ordering = ['shift', 'method']
        constraints = [
            models.UniqueConstraint(
                fields=['shift', 'method'],
                condition=models.Q(is_deleted=False),
                name='uniq_shift_method_active',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['shift_uuid'] = str(self.shift.uuid) if self.shift else None
        return data

    def __str__(self):
        return f"{self.shift_id}:{self.method} exp={self.expected_amount}"


class CashboxExpenseCategory(SyncMixin, models.Model):
    """Catalog of cashbox (drawer) expense categories. Separate from
    hr.ExpenseCategory — these are POS/drawer expenses, not payroll/HR."""
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SyncManager()

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class CashboxExpense(SyncMixin, models.Model):
    """Money paid OUT of a shift's cash drawer.

    Recipient is at most one of user / supplier (or none). When the recipient is
    a supplier, the service layer also writes a SupplierTransaction so the
    supplier balance reflects cash paid from the drawer (see P5).
    """
    shift = models.ForeignKey(
        'base.Shift', on_delete=models.CASCADE, related_name='cashbox_expenses',
    )
    category = models.ForeignKey(
        CashboxExpenseCategory, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='expenses',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    comment = models.TextField(blank=True, default='')
    recipient_user = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cashbox_expenses_received',
    )
    recipient_supplier = models.ForeignKey(
        'stock.Supplier', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cashbox_expenses_received',
    )
    created_by = models.ForeignKey(
        'base.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cashbox_expenses_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Branch-owned money.
    SYNC_WRITE_DENYLIST = frozenset({'amount'})

    objects = SyncManager()

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # At most one recipient kind.
            models.CheckConstraint(
                condition=~(
                    models.Q(recipient_user__isnull=False)
                    & models.Q(recipient_supplier__isnull=False)
                ),
                name='cashboxexpense_single_recipient',
            ),
        ]

    def to_sync_dict(self):
        data = super().to_sync_dict()
        data['shift_uuid'] = str(self.shift.uuid) if self.shift else None
        # Distinct uuid key (category_uuid is globally claimed by base.Category).
        data['cashbox_category_uuid'] = str(self.category.uuid) if self.category else None
        data['recipient_user_uuid'] = str(self.recipient_user.uuid) if self.recipient_user else None
        data['recipient_supplier_uuid'] = str(self.recipient_supplier.uuid) if self.recipient_supplier else None
        data['created_by_uuid'] = str(self.created_by.uuid) if self.created_by else None
        return data

    def __str__(self):
        return f"CashboxExpense {self.amount} (shift {self.shift_id})"
