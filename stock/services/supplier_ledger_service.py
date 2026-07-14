"""Supplier ledger: record purchase debt + pay suppliers, with a full audit
trail (SupplierTransaction). Positive balance = we owe the supplier.

Every change is row-locked and writes a ledger row with balance_before/after so
the supplier balance always reconciles. Payments from SAFE/BANK also debit the
treasury (a SUPPLIER_PAYMENT ledger row); paying from the shift drawer goes
through a CashboxExpense (P4), which calls record_drawer_payment here.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction

from base.helpers.response import ServiceResponse
from stock.models import Supplier, SupplierTransaction

# Types that REDUCE what we owe.
_DEBT_MINUS = {SupplierTransaction.Type.PAYMENT, SupplierTransaction.Type.RETURN}


def _to_dec(value):
    try:
        return Decimal(str(value or 0)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return None


class SupplierLedgerService:

    @staticmethod
    @transaction.atomic
    def _post(supplier_id, txn_type, amount, *, source_account='', fee=0,
              reference_type='', reference_id=None, note='', performed_by=None):
        """Row-lock the supplier, write a ledger row, move current_balance.
        Returns the SupplierTransaction, or None if the supplier is missing."""
        supplier = (
            Supplier.objects.select_for_update()
            .filter(id=supplier_id, is_deleted=False).first()
        )
        if not supplier:
            return None
        amt = _to_dec(amount) or Decimal('0')
        before = supplier.current_balance or Decimal('0')
        after = before - amt if txn_type in _DEBT_MINUS else before + amt
        txn = SupplierTransaction.objects.create(
            supplier=supplier, type=txn_type, amount=amt,
            balance_before=before, balance_after=after,
            source_account=source_account or '', fee=_to_dec(fee) or Decimal('0'),
            reference_type=reference_type or '', reference_id=reference_id,
            note=note or '', performed_by=performed_by,
        )
        supplier.current_balance = after
        supplier.save(update_fields=['current_balance', 'updated_at',
                                     'synced_at', 'sync_version'])
        return txn

    @classmethod
    def record_purchase(cls, supplier_id, amount, reference_type='',
                        reference_id=None, performed_by=None, note=''):
        """We received goods worth `amount` → we now owe the supplier more."""
        return cls._post(
            supplier_id, SupplierTransaction.Type.PURCHASE, amount,
            reference_type=reference_type, reference_id=reference_id,
            performed_by=performed_by, note=note,
        )

    @classmethod
    def record_purchase_order_payment(cls, supplier_id, amount,
                                      purchase_order_id, note=''):
        """Record a legacy PO payment whose funding account is unspecified.

        PurchaseOrderService historically only received an amount, not a SAFE,
        BANK, or DRAWER source.  We must not invent a treasury movement, but the
        supplier balance still belongs in the locked append-only ledger rather
        than a read/modify/save shortcut.
        """
        return cls._post(
            supplier_id, SupplierTransaction.Type.PAYMENT, amount,
            reference_type='PurchaseOrder', reference_id=purchase_order_id,
            note=note,
        )

    @classmethod
    def record_drawer_payment(cls, supplier_id, amount, reference_id=None,
                              performed_by=None, note=''):
        """Cash paid to a supplier out of a shift drawer (a CashboxExpense, P4).
        The drawer deduction is the expense itself; this only moves the supplier
        balance down."""
        return cls._post(
            supplier_id, SupplierTransaction.Type.PAYMENT, amount,
            source_account=SupplierTransaction.SourceAccount.DRAWER,
            reference_type='CashboxExpense', reference_id=reference_id,
            performed_by=performed_by, note=note,
        )

    @classmethod
    @transaction.atomic
    def pay_supplier(cls, supplier_id, amount, source_account='SAFE',
                     commission=0, note='', performed_by=None):
        """Pay a supplier from SAFE or BANK: debits the treasury (with optional
        bank commission) and reduces what we owe. DRAWER payments go through a
        cashbox expense instead (so the drawer isn't double-counted)."""
        supplier = Supplier.objects.filter(id=supplier_id, is_deleted=False).first()
        if not supplier:
            return ServiceResponse.not_found('Supplier not found')
        src = (source_account or 'SAFE').upper()
        amt = _to_dec(amount)
        if amt is None or amt <= 0:
            return ServiceResponse.validation_error(
                errors={'amount': 'Amount must be greater than 0'})
        comm = _to_dec(commission) or Decimal('0')

        if src == 'DRAWER':
            return ServiceResponse.validation_error(errors={
                'source_account': 'Pay from the drawer via a cashbox expense, not here'})
        if src not in ('SAFE', 'BANK'):
            return ServiceResponse.validation_error(
                errors={'source_account': 'Must be SAFE or BANK'})

        from base.services.treasury_service import TreasuryService
        from base.models import TreasuryTransaction
        # Commission only applies to bank payments.
        fee = comm if src == 'BANK' else Decimal('0')
        res, status = TreasuryService.record_expense(
            account_kind=src, amount=amt, fee=fee,
            description=f'Supplier payment: {supplier.name}',
            performed_by=performed_by,
            txn_type=TreasuryTransaction.Type.SUPPLIER_PAYMENT,
            reference_type='Supplier', reference_id=supplier.id,
        )
        if status >= 400:
            return res, status

        txn = cls._post(
            supplier_id, SupplierTransaction.Type.PAYMENT, amt,
            source_account=src, fee=fee, reference_type='TreasuryPayment',
            performed_by=performed_by, note=note,
        )
        return ServiceResponse.success(
            data={'paid': str(amt), 'fee': str(fee),
                  'supplier_balance': str(txn.balance_after if txn else supplier.current_balance)},
            message='Supplier paid')

    @staticmethod
    def history(supplier_id, page=1, per_page=20):
        qs = SupplierTransaction.objects.filter(
            supplier_id=supplier_id, is_deleted=False,
        ).select_related('performed_by')
        total = qs.count()
        items = qs[(page - 1) * per_page: page * per_page]
        return ServiceResponse.success(data={
            'transactions': [{
                'id': t.id,
                'type': t.type,
                'amount': str(t.amount),
                'balance_after': str(t.balance_after),
                'source_account': t.source_account,
                'fee': str(t.fee),
                'reference_type': t.reference_type,
                'reference_id': t.reference_id,
                'note': t.note,
                'created_at': t.created_at.isoformat() if t.created_at else None,
            } for t in items],
            'pagination': {'page': page, 'per_page': per_page, 'total': total},
        })
