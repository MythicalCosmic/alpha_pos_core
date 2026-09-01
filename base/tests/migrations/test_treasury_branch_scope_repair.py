import importlib

import pytest
from django.apps import apps
from django.utils import timezone

from base.models import AuditLog, TreasuryAccount, TreasuryTransaction
from hr.models import Expense, ExpenseTransition


pytestmark = pytest.mark.django_db


def _transaction(account, *, branch_id, amount='100'):
    return TreasuryTransaction.objects.create(
        account=account,
        type=TreasuryTransaction.Type.EXPENSE,
        delta=f'-{amount}',
        balance_before=amount,
        balance_after='0',
        branch_id=branch_id,
    )


def test_repair_inherits_account_branch_without_changing_money():
    migration = importlib.import_module(
        'base.migrations.0064_repair_treasury_branch_scope'
    )
    account = TreasuryAccount.objects.create(
        kind='SAFE', balance='0', branch_id='branch-a',
    )
    transaction = _transaction(account, branch_id='cloud')
    expense = Expense.objects.create(
        amount='100',
        expense_date=timezone.localdate(),
        status=Expense.Status.PAID,
        requested_source=Expense.Source.SAFE,
        treasury_transaction=transaction,
        branch_id='cloud',
    )
    transition = ExpenseTransition.objects.create(
        expense=expense,
        new_status=Expense.Status.PAID,
        branch_id='cloud',
    )

    migration.repair_treasury_branch_scope(apps, None)

    transaction.refresh_from_db()
    expense.refresh_from_db()
    transition.refresh_from_db()
    assert transaction.branch_id == 'branch-a'
    assert transaction.delta == -100
    assert transaction.balance_before == 100
    assert transaction.balance_after == 0
    assert expense.branch_id == 'branch-a'
    assert expense.amount == 100
    assert transition.branch_id == 'branch-a'
    audit = AuditLog.objects.get(
        action=AuditLog.Action.FINANCIAL_REPAIR,
        target_type='TreasuryTransaction',
        target_id=transaction.id,
    )
    assert audit.branch_id == 'branch-a'
    assert audit.metadata == {
        'repair': 'TREASURY_BRANCH_SCOPE',
        'from_branch_id': 'cloud',
        'to_branch_id': 'branch-a',
        'account_id': account.id,
        'expense_ids': [expense.id],
        'amounts_unchanged': True,
    }


def test_repair_leaves_concrete_cross_branch_mismatch_for_review():
    migration = importlib.import_module(
        'base.migrations.0064_repair_treasury_branch_scope'
    )
    account = TreasuryAccount.objects.create(
        kind='BANK', balance='0', branch_id='branch-a',
    )
    transaction = _transaction(account, branch_id='branch-b')

    migration.repair_treasury_branch_scope(apps, None)

    transaction.refresh_from_db()
    assert transaction.branch_id == 'branch-b'
    assert not AuditLog.objects.filter(
        action=AuditLog.Action.FINANCIAL_REPAIR,
        target_type='TreasuryTransaction',
        target_id=transaction.id,
    ).exists()
