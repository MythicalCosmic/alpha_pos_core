from dataclasses import dataclass
from decimal import Decimal

from stock.models import SupplierTransaction


_DEBT_DECREASE_TYPES = {
    SupplierTransaction.Type.PAYMENT,
    SupplierTransaction.Type.RETURN,
}


def _is_whole_uzs(value):
    value = Decimal(value or 0)
    return value.is_finite() and value == value.to_integral_value()


@dataclass(frozen=True)
class SupplierLedgerEvidence:
    supplier_id: int
    stored_balance: Decimal
    ledger_balance: Decimal
    valid: bool
    currency_supported: bool


def validate_supplier_ledgers(suppliers):
    suppliers = list(suppliers)
    supplier_by_id = {supplier.id: supplier for supplier in suppliers}
    rows_by_supplier = {supplier.id: [] for supplier in suppliers}
    rows = SupplierTransaction.objects.filter(
        supplier_id__in=supplier_by_id,
        is_deleted=False,
    ).order_by('supplier_id', 'created_at', 'id')
    for row in rows:
        rows_by_supplier[row.supplier_id].append(row)

    result = {}
    for supplier in suppliers:
        stored = Decimal(supplier.current_balance or 0)
        running = Decimal('0')
        valid = _is_whole_uzs(stored)
        ledger_rows = rows_by_supplier[supplier.id]
        if ledger_rows and Decimal(ledger_rows[0].balance_before) != 0:
            valid = False
        for row in ledger_rows:
            before = Decimal(row.balance_before)
            amount = Decimal(row.amount)
            after = Decimal(row.balance_after)
            fee = Decimal(row.fee)
            expected = (
                before - amount
                if row.type in _DEBT_DECREASE_TYPES
                else before + amount
            )
            if (
                row.branch_id != supplier.branch_id
                or row.type not in SupplierTransaction.Type.values
                or amount <= 0
                or (
                    fee > 0
                    and row.type != SupplierTransaction.Type.PAYMENT
                )
                or (
                    fee < 0
                    and row.type != SupplierTransaction.Type.PAYMENT_REVERSAL
                )
                or before != running
                or after != expected
                or not all(_is_whole_uzs(value) for value in (
                    amount, row.fee, before, after,
                ))
            ):
                valid = False
            running = after
        if running != stored:
            valid = False
        result[supplier.id] = SupplierLedgerEvidence(
            supplier_id=supplier.id,
            stored_balance=stored,
            ledger_balance=running,
            valid=valid,
            currency_supported=supplier.currency == 'UZS',
        )
    return result
