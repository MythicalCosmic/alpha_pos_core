"""Per-shift cashbox + shift settlement models.

Design (see the Money & Shift Logic spec):
  * The cashier's live cash is a PER-SHIFT drawer, not the old global
    CashRegister. It's *derived* (cash sales - cash expenses - cash returns
    since shift start), so there is no stored running-balance column to drift.
  * At shift close the cashier counts each tender type; ShiftPaymentTotal
    freezes expected/counted/difference per method and manager reconciliation
    confirms that evidence and posts every confirmed tender to SAFE exactly
    once. Later inkassa is physical register movement/audit only.
  * CashboxExpense is money paid OUT of the drawer (its own model, not hr.Expense).

Money fields carry SYNC_WRITE_DENYLIST so a branch only ever owns its own
drawer figures — a pulled peer/cloud copy can't rewrite them (the cloud, the
trusted aggregator, still accepts them). Mirrors base.Order.
"""
from decimal import Decimal, InvalidOperation

from django.db import models

from base.models import SyncMixin, SyncManager


# Concrete tender types a drawer is counted in. MIXED is never stored here —
# a mixed-tender order is split into its component OrderPayment rows, each of
# which lands under its own method below. Kept loose (CharField, no DB choices)
# because PaymentMethodConfig is operator-editable.
#
# 'CARD' MUST stay in step with Order.PaymentMethod: drawer.expected_payment_totals
# seeds its dict from this tuple and `totals.get(method, 0)` would otherwise mint a
# bucket for an unlisted method, which then becomes a real ShiftPaymentTotal row
# (unique(shift, method)) that nothing else knows about.
PAYMENT_METHODS = ('CASH', 'UZCARD', 'HUMO', 'CARD', 'PAYME')


class ShiftPaymentTotal(SyncMixin, models.Model):
    """Per-(shift, method) settlement row.

    expected  = system figure (Σ OrderPayment for the method in the shift window,
                minus cash expenses for CASH).
    counted   = what the cashier physically counted at close (blind).
    confirmed = the manager's final accepted audit figure.
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
    # A close row is immutable evidence once created. Cloud receive may create
    # it once from the owning branch, but a later branch push cannot rewrite or
    # tombstone it after the manager has posted the shift. The one trusted
    # cloud->branch update is manager ``confirmed_amount`` (see receiver).
    _sync_append_only = True
    SYNC_APPEND_ONLY_TRUSTED_UPDATE_FIELDS = frozenset({'confirmed_amount'})

    # On cloud->branch pull preserve locally frozen close figures, while still
    # delivering the cloud manager's confirmation.
    SYNC_WRITE_DENYLIST = frozenset({
        'expected_amount', 'counted_amount', 'difference',
    })
    # On branch->cloud create confirmed defaults to zero; only the manager API
    # is authoritative for this field. Updates are refused wholesale by the
    # append-only policy.
    SYNC_DENY_FROM_BRANCH = frozenset({'confirmed_amount'})

    # (shift, method) prevents duplicate active tender rows. A same-natural-key
    # event under another UUID is a conflict, not an overwrite opportunity: the
    # receiver preserves the first immutable row and manifest verification
    # fails closed when the branch committed a different UUID.
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

    @classmethod
    def branch_sync_create_allowed(cls, *, uuid_val, values, resolved_fks):
        """After close, accept only the exact SPT committed by the manifest.

        Append-only protects existing rows, but without this guard a branch
        could add a brand-new tender UUID/method after manager posting and make
        shift analytics disagree with the immutable treasury ledger.
        """
        shift = resolved_fks.get('shift')
        manifest = getattr(shift, 'settlement_manifest', None) if shift else None
        if not manifest:
            return True

        def money(value):
            try:
                amount = Decimal(str(value)).quantize(Decimal('0.01'))
            except (InvalidOperation, TypeError, ValueError):
                return None
            return str(amount) if amount.is_finite() else None

        candidate = {
            'uuid': str(uuid_val),
            'method': str(values.get('method') or ''),
            'expected': money(values.get('expected_amount')),
            'counted': money(values.get('counted_amount')),
            'difference': money(values.get('difference')),
        }
        return candidate in (manifest.get('tenders') or [])

    def __str__(self):
        return f"{self.shift_id}:{self.method} exp={self.expected_amount}"


class CashboxExpenseCategory(SyncMixin, models.Model):
    SYNC_PULL_SCOPE = 'global'
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
    REGISTER_COMMAND_MARKER = '[ALPHAPOS_CASHBOX_COMMAND_V1]'

    shift = models.ForeignKey(
        'base.Shift', on_delete=models.CASCADE, related_name='cashbox_expenses',
    )
    category = models.ForeignKey(
        CashboxExpenseCategory, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='expenses',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # Cloud-created drawer expenses are durable commands. The owning branch
    # applies them to CashRegister on pull; ordinary till-created expenses are
    # already applied locally and keep this False.
    register_command = models.BooleanField(default=False)
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
    SYNC_DENY_FROM_BRANCH = frozenset({'register_command'})

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

    @classmethod
    def command_comment(cls, comment=''):
        return f'{cls.REGISTER_COMMAND_MARKER}\n{str(comment or "")}'

    @classmethod
    def visible_comment(cls, comment=''):
        text = str(comment or '')
        prefix = f'{cls.REGISTER_COMMAND_MARKER}\n'
        return text[len(prefix):] if text.startswith(prefix) else text

    @classmethod
    def from_sync_dict(cls, data, branch_id=None):
        instance, action = super().from_sync_dict(data, branch_id=branch_id)
        if instance is not None and not instance.is_deleted and (
            instance.register_command
            or str(instance.comment or '').startswith(cls.REGISTER_COMMAND_MARKER)
        ):
            # One cumulative register adjustment covers both remote expenses
            # and inkassa. It is idempotent if the same pull record is replayed.
            from base.models import Inkassa
            applied = Inkassa._apply_pending_register_commands(instance.branch_id)
            if not applied:
                return instance, 'deferred'
        return instance, action

    def __str__(self):
        return f"CashboxExpense {self.amount} (shift {self.shift_id})"
