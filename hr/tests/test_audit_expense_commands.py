from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone

from base.models import Shift, TreasuryAccount, TreasuryTransaction
from cashbox.models import CashboxExpense
from cashbox.services.expense_service import CashboxExpenseService
from hr.models import Expense, ExpenseCategory, ExpenseTransition
from hr.services.expense_service import ExpenseService

pytestmark = pytest.mark.django_db


@pytest.fixture
def expense_setup(admin_user, settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    admin_user.branch_id = 'branch1'
    admin_user.save(update_fields=['branch_id'])
    category = ExpenseCategory.objects.create(
        name='Audit operating', code='AUDIT-OPERATING', allowed_sources=['BANK', 'DRAWER'],
        reporting_group='OPERATING', branch_id='branch1',
    )
    account = TreasuryAccount.objects.create(kind='BANK', balance=1000, branch_id='branch1')
    return admin_user, category, account


def test_direct_payment_accepts_percentage_without_absolute_fee(expense_setup):
    actor, category, account = expense_setup
    result, status = ExpenseService.direct_pay(
        actor=actor, action_id=uuid4(), category_id=category.pk,
        requested_source='BANK', amount_uzs=100, fee_percent=1,
    )
    assert status < 400, result
    account.refresh_from_db()
    assert account.balance == Decimal('899')
    assert Expense.objects.get().status == Expense.Status.PAID


@pytest.mark.parametrize('extra', [{'fee_uzs': -1}, {'fee_uzs': 2, 'fee_percent': 1}])
def test_failed_direct_payment_leaves_no_approved_expense(expense_setup, extra):
    actor, category, account = expense_setup
    result, status = ExpenseService.direct_pay(
        actor=actor, action_id=uuid4(), category_id=category.pk,
        requested_source='BANK', amount_uzs=100, **extra,
    )
    assert status == 422, result
    assert not Expense.objects.exists()
    assert not ExpenseTransition.objects.exists()
    assert not TreasuryTransaction.objects.exists()
    account.refresh_from_db()
    assert account.balance == 1000


def test_drawer_reversal_cannot_change_closed_shift(expense_setup, cashier_user):
    actor, category, _ = expense_setup
    cashier_user.branch_id = 'branch1'
    cashier_user.save(update_fields=['branch_id'])
    shift = Shift.objects.create(user=cashier_user, status=Shift.Status.COMPLETED,
                                 branch_id='branch1', start_time=timezone.now())
    original = CashboxExpense.objects.create(
        shift=shift, canonical_category=category, amount=10,
        created_by=actor, branch_id='branch1',
    )
    result, status = CashboxExpenseService.reverse_payment(original, actor, 'Correction')
    assert status == 409, result
    assert CashboxExpense.objects.count() == 1
