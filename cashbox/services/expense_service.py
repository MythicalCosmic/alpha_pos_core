"""Cashbox (drawer) expenses — money paid OUT of a shift's cash drawer.

Recording an expense reduces the (derived) drawer automatically because
drawer_cash() subtracts cashbox expenses. When the recipient is a supplier we
also post a PAYMENT to the supplier ledger so what we owe drops.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction

from base.helpers.response import ServiceResponse
from base.models import Shift
from cashbox.models import CashboxExpense, CashboxExpenseCategory


def _to_dec(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return None


class CashboxExpenseService:

    @staticmethod
    @transaction.atomic
    def create(shift_id, amount, category_id=None, comment='',
               recipient_user_id=None, recipient_supplier_id=None, created_by=None):
        shift = Shift.objects.filter(id=shift_id, is_deleted=False).first()
        if not shift:
            return ServiceResponse.not_found('Shift not found')
        if shift.status != Shift.Status.ACTIVE:
            return ServiceResponse.error('Can only record expenses on an active shift')

        amt = _to_dec(amount)
        if amt is None or amt <= 0:
            return ServiceResponse.validation_error(
                errors={'amount': 'Amount must be greater than 0'})
        if recipient_user_id and recipient_supplier_id:
            return ServiceResponse.validation_error(
                errors={'recipient': 'Choose at most one recipient (user OR supplier)'})

        category = None
        if category_id:
            category = CashboxExpenseCategory.objects.filter(
                id=category_id, is_deleted=False).first()
            if not category:
                return ServiceResponse.not_found('Expense category not found')

        expense = CashboxExpense.objects.create(
            shift=shift, category=category, amount=amt, comment=comment or '',
            recipient_user_id=recipient_user_id,
            recipient_supplier_id=recipient_supplier_id,
            created_by=created_by,
        )

        # Paying a supplier from the drawer also moves the supplier balance down.
        if recipient_supplier_id:
            from stock.services.supplier_ledger_service import SupplierLedgerService
            SupplierLedgerService.record_drawer_payment(
                recipient_supplier_id, amt, reference_id=expense.id,
                performed_by=created_by, note=comment or '',
            )

        return ServiceResponse.created(
            data={'id': expense.id, 'amount': str(amt), 'shift_id': shift.id},
            message='Expense recorded')

    @staticmethod
    def list_for_shift(shift_id):
        rows = (CashboxExpense.objects.filter(shift_id=shift_id, is_deleted=False)
                .select_related('category', 'recipient_user', 'recipient_supplier'))
        return ServiceResponse.success(data=[{
            'id': e.id,
            'amount': str(e.amount),
            'comment': e.comment,
            'category': e.category.name if e.category else None,
            'recipient_user': (f'{e.recipient_user.first_name} {e.recipient_user.last_name}'.strip()
                               if e.recipient_user else None),
            'recipient_supplier': e.recipient_supplier.name if e.recipient_supplier else None,
            'created_at': e.created_at.isoformat() if e.created_at else None,
        } for e in rows])


class CashboxCategoryService:

    @staticmethod
    def list():
        rows = CashboxExpenseCategory.objects.filter(is_deleted=False, is_active=True)
        return ServiceResponse.success(data=[
            {'id': c.id, 'name': c.name, 'sort_order': c.sort_order} for c in rows])

    @staticmethod
    def create(name, sort_order=0):
        if not (name or '').strip():
            return ServiceResponse.validation_error(errors={'name': 'Name is required'})
        c = CashboxExpenseCategory.objects.create(name=name.strip(), sort_order=sort_order or 0)
        return ServiceResponse.created(data={'id': c.id, 'name': c.name})
