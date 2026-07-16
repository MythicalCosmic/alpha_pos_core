"""Branch-safety regressions for legacy cashbox supplier recipients."""
from decimal import Decimal

import pytest
from django.utils import timezone

from base.models import CashRegister, Order, OrderPayment, Shift, User
from cashbox.services.expense_service import CashboxExpenseService
from stock.models import Supplier


pytestmark = pytest.mark.django_db


def _funded_shift(branch='branch-legacy'):
    user = User.objects.create(
        first_name='Cash', last_name='Ier', email=f'{branch}@example.test',
        password='x', role='CASHIER', status='ACTIVE', branch_id=branch,
    )
    shift = Shift.objects.create(
        user=user, start_time=timezone.now(), status='ACTIVE', branch_id=branch,
    )
    order = Order.objects.create(
        user=user, cashier=user, status='COMPLETED', is_paid=True,
        paid_at=timezone.now(), total_amount=Decimal('20000'),
        payment_method='CASH', branch_id=branch,
    )
    OrderPayment.objects.create(
        order=order, method='CASH', amount=Decimal('20000'), branch_id=branch,
    )
    CashRegister.objects.update_or_create(
        branch_id=branch,
        defaults={'current_balance': Decimal('20000'), 'is_deleted': False},
    )
    return user, shift


def test_blank_branch_legacy_supplier_is_claimed_by_expense_branch():
    user, shift = _funded_shift()
    supplier = Supplier.objects.create(
        name='Legacy Veg Co', current_balance=Decimal('50000'),
        branch_id='temporary-setup-branch',
    )
    # Simulate a row created before supplier branch ownership was introduced.
    Supplier.objects.filter(pk=supplier.pk).update(branch_id='')

    response, status = CashboxExpenseService.create(
        shift.id, Decimal('20000'), recipient_supplier_id=supplier.id,
        created_by=user,
    )

    assert status == 201, response
    supplier.refresh_from_db()
    assert supplier.branch_id == shift.branch_id
    assert supplier.current_balance == Decimal('30000.00')


def test_supplier_owned_by_another_branch_is_still_rejected():
    user, shift = _funded_shift('branch-a')
    supplier = Supplier.objects.create(
        name='Other Branch Supplier', current_balance=Decimal('50000'),
        branch_id='branch-b',
    )

    response, status = CashboxExpenseService.create(
        shift.id, Decimal('1000'), recipient_supplier_id=supplier.id,
        created_by=user,
    )

    assert status == 422
    assert response['errors']['recipient_supplier_id'] == (
        'Supplier is not in this branch'
    )
    supplier.refresh_from_db()
    assert supplier.current_balance == Decimal('50000.00')
