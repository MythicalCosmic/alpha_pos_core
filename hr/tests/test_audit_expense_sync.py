from uuid import uuid4

import pytest

from base.services.sync.receiver import CloudReceiver
from hr.models import Expense, ExpenseTransition
from hr.services.expense_service import ExpenseService
from . import test_audit_expense_commands as expense_fixtures

expense_setup = expense_fixtures.expense_setup

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('changes', [
    {'amount': '900'}, {'status': 'VOIDED'}, {'is_deleted': True},
    {'requested_source': 'DRAWER', 'payment_action_id': None, 'amount': '900'},
])
def test_paid_cloud_expense_cannot_be_rewritten_by_sync(expense_setup, changes):
    actor, category, account = expense_setup
    result, status = ExpenseService.direct_pay(
        actor=actor, action_id=uuid4(), category_id=category.pk,
        requested_source='BANK', amount_uzs=100)
    assert status < 400, result
    expense = Expense.objects.get()
    payload = expense.to_sync_dict()
    payload.update(changes, sync_version=expense.sync_version + 100)
    applied = CloudReceiver._create_or_update(Expense, payload, 'branch1')
    assert getattr(applied, 'disposition', 'acknowledged') == 'rejected'
    assert applied.reason_code == 'EXPENSE_COMMAND_REQUIRED'
    expense.refresh_from_db()
    account.refresh_from_db()
    assert expense.amount == 100
    assert expense.status == 'PAID'
    assert not expense.is_deleted
    assert account.balance == 900


@pytest.mark.parametrize('source', ['SAFE', 'BANK'])
def test_branch_cannot_forge_treasury_expense(expense_setup, source):
    applied = CloudReceiver._create_or_update(Expense, {
        'uuid': str(uuid4()), 'sync_version': 1, 'amount': '100',
        'expense_date': '2026-09-23', 'status': 'PAID', 'requested_source': source,
    }, 'branch1')
    assert getattr(applied, 'disposition', 'acknowledged') == 'rejected'
    assert applied.reason_code == 'EXPENSE_COMMAND_REQUIRED'
    assert not Expense.objects.exists()


def test_branch_cannot_forge_approval_audit_transition(expense_setup):
    applied = CloudReceiver._create_or_update(ExpenseTransition, {
        'uuid': str(uuid4()), 'expense_uuid': str(uuid4()),
        'previous_status': 'PENDING', 'new_status': 'APPROVED',
    }, 'branch1')
    assert getattr(applied, 'disposition', 'acknowledged') == 'rejected'
    assert applied.reason_code == 'EXPENSE_COMMAND_REQUIRED'
    assert not ExpenseTransition.objects.exists()


@pytest.mark.parametrize('source', ['', 'DRAWER'])
def test_legacy_branch_expense_evidence_is_still_accepted(expense_setup, source):
    applied = CloudReceiver._create_or_update(Expense, {
        'uuid': str(uuid4()), 'sync_version': 1, 'amount': '100',
        'expense_date': '2026-09-23', 'status': 'PENDING', 'requested_source': source,
    }, 'branch1')
    _, action = applied
    assert action == 'created'
    assert Expense.objects.get().branch_id == 'branch1'


def test_till_can_still_update_its_drawer_expense_after_cloud_history(expense_setup):
    """The cloud writes history for drawer expenses it receives; the till's
    later edit of its own drawer expense must still apply."""
    expense_uuid = str(uuid4())
    payload = {
        'uuid': expense_uuid, 'sync_version': 1, 'amount': '100',
        'expense_date': '2026-09-23', 'status': 'PAID', 'requested_source': 'DRAWER',
    }
    CloudReceiver._create_or_update(Expense, dict(payload), 'branch1')
    expense = Expense.objects.get(uuid=expense_uuid)
    ExpenseTransition.objects.create(
        expense=expense, previous_status='', new_status='PAID', branch_id='branch1',
    )
    applied = CloudReceiver._create_or_update(
        Expense, dict(payload, amount='150', sync_version=2), 'branch1',
    )
    assert getattr(applied, 'disposition', 'acknowledged') != 'rejected'
    expense.refresh_from_db()
    assert expense.amount == 150


def test_cloud_voided_drawer_expense_stays_protected(expense_setup):
    expense_uuid = str(uuid4())
    payload = {
        'uuid': expense_uuid, 'sync_version': 1, 'amount': '100',
        'expense_date': '2026-09-23', 'status': 'PAID', 'requested_source': 'DRAWER',
    }
    CloudReceiver._create_or_update(Expense, dict(payload), 'branch1')
    Expense.objects.filter(uuid=expense_uuid).update(void_action_id=uuid4(), status='VOIDED')
    applied = CloudReceiver._create_or_update(
        Expense, dict(payload, sync_version=5), 'branch1',
    )
    assert applied.reason_code == 'EXPENSE_COMMAND_REQUIRED'
    assert Expense.objects.get(uuid=expense_uuid).status == 'VOIDED'
