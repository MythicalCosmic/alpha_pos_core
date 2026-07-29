from io import StringIO
from datetime import timezone as datetime_timezone
import json
import re

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _datetime_text(value):
    return (
        value.astimezone(datetime_timezone.utc)
        .isoformat(timespec='microseconds')
        .replace('+00:00', 'Z')
    )


def _user(email, branch='branch1'):
    from base.models import User

    return User.objects.create(
        first_name='History',
        last_name='Repair',
        email=email,
        password='x',
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
        branch_id=branch,
    )


def _legacy_change_shift(user, *, total, tendered, counted):
    from datetime import timedelta
    from decimal import Decimal
    from base.models import Order, OrderPayment, Shift
    from cashbox.models import ShiftPaymentTotal

    now = timezone.now()
    shift = Shift.objects.create(
        user=user,
        start_time=now - timedelta(hours=2),
        end_time=now - timedelta(hours=1),
        status=Shift.Status.ENDED,
        treasury_settlement_eligible=False,
        settlement_manifest={},
        branch_id=user.branch_id,
    )
    paid_at = now - timedelta(minutes=90)
    order = Order.objects.create(
        user=user,
        cashier=user,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        paid_at=paid_at,
        display_id=1,
        subtotal=total,
        total_amount=total,
        branch_id=user.branch_id,
    )
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.CASH,
        amount=tendered,
        branch_id=user.branch_id,
    )
    expected = Decimal(tendered)
    difference = Decimal(counted) - expected
    row = ShiftPaymentTotal.objects.create(
        shift=shift,
        method='CASH',
        expected_amount=expected,
        counted_amount=counted,
        confirmed_amount='0.00',
        difference=difference,
        branch_id=user.branch_id,
    )
    return shift, row


def _history(tmp_path, settings):
    from datetime import timedelta
    from decimal import Decimal
    from base.models import CashRegister, Shift
    from cashbox.models import CashboxExpense

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch1'

    register = CashRegister.objects.create(
        branch_id='branch1',
        current_balance='1000.00',
        remote_cash_out_applied_total='7.00',
    )
    user = _user('legacy-repair-a@test.local')
    user_b = _user('legacy-repair-b@test.local')
    shift_a, row_a = _legacy_change_shift(
        user,
        total='100.00',
        tendered='195.00',
        counted='100.00',
    )
    shift_b, row_b = _legacy_change_shift(
        user_b,
        total='50.00',
        tendered='51.00',
        counted='50.00',
    )

    expense_shift_start = timezone.now() - timedelta(days=3)
    expense_shift_end = timezone.now() - timedelta(days=2)
    expense_shift = Shift.objects.create(
        user=user,
        start_time=expense_shift_start,
        end_time=expense_shift_end,
        status=Shift.Status.ENDED,
        branch_id='branch1',
    )
    expenses = [
        CashboxExpense.objects.create(
            shift=expense_shift,
            amount='100.00',
            comment='legacy drawer payout',
            register_command=False,
            branch_id='branch1',
        ),
        CashboxExpense.objects.create(
            shift=expense_shift,
            amount='50.00',
            comment='legacy drawer payout',
            register_command=False,
            branch_id='branch1',
        ),
    ]
    # auto_now_add uses insertion time; reproduce real historical evidence by
    # placing each expense inside its now-ended shift window.
    historical_created = expense_shift_start + timedelta(hours=1)
    for index, expense in enumerate(expenses):
        type(expense)._base_manager.filter(pk=expense.pk).update(
            created_at=historical_created + timedelta(minutes=index),
        )
        expense.refresh_from_db()

    _stamp_settlement_rows_as_synced([row_a, row_b])

    plan = {
        'version': 2,
        'repair_id': 'restaurant-july-2026-financial-history-v1',
        'operator': 'codex-reviewed restaurant owner',
        'branch_id': 'branch1',
        'register_repair': {
            'register_uuid': str(register.uuid),
            'legacy_created_before': _datetime_text(
                expense_shift_end + timedelta(hours=1)
            ),
            'expenses': [
                {
                    'uuid': str(row.uuid),
                    'amount': f'{Decimal(row.amount):.2f}',
                    'shift_uuid': str(row.shift.uuid),
                    'created_at': _datetime_text(row.created_at),
                }
                for row in expenses
            ],
            'expected_expense_total': '150.00',
        },
        'shift_payment_total_repairs': [
            {
                'shift_uuid': str(shift_a.uuid),
                'payment_total_uuid': str(row_a.uuid),
                'expected_before': '195.00',
                'expected_after': '100.00',
                'counted_amount': '100.00',
                'difference_before': '-95.00',
                'difference_after': '0.00',
                'customer_change_excluded': '95.00',
            },
            {
                'shift_uuid': str(shift_b.uuid),
                'payment_total_uuid': str(row_b.uuid),
                'expected_before': '51.00',
                'expected_after': '50.00',
                'counted_amount': '50.00',
                'difference_before': '-1.00',
                'difference_after': '0.00',
                'customer_change_excluded': '1.00',
            },
        ],
    }
    plan_path = tmp_path / 'financial-repair-plan.json'
    plan_path.write_text(json.dumps(plan), encoding='utf-8')
    return {
        'register': register,
        'shifts': [shift_a, shift_b],
        'rows': [row_a, row_b],
        'expenses': expenses,
        'plan': plan,
        'plan_path': plan_path,
    }


def _run(plan_path, **kwargs):
    out = StringIO()
    with open(plan_path, encoding='utf-8') as plan_file:
        operator = json.load(plan_file).get('operator', '')
    call_command(
        'repair_financial_history',
        plan=str(plan_path),
        branch='branch1',
        operator=kwargs.pop('operator', operator),
        stdout=out,
        **kwargs,
    )
    text = out.getvalue()
    match = re.search(r'Evidence fingerprint: ([0-9a-f]{64})', text)
    assert match, text
    return text, match.group(1)


def _stamp_settlement_rows_as_synced(rows):
    from datetime import timedelta
    from cashbox.models import ShiftPaymentTotal

    stamp = timezone.now() - timedelta(minutes=5)
    for row in rows:
        ShiftPaymentTotal._base_manager.filter(pk=row.pk).update(
            synced_at=stamp,
            sync_version=7,
        )
        row.refresh_from_db()


def test_dry_run_previews_exact_register_and_change_repairs_without_writes(
    tmp_path, settings,
):
    from decimal import Decimal
    from base.models import AuditLog

    history = _history(tmp_path, settings)

    text, _fingerprint = _run(history['plan_path'])

    assert (
        'Register repair: 2 expense(s), 150.00; '
        'balance 1000.00 -> 850.00'
    ) in text
    assert 'expected 195.00 -> 100.00' in text
    assert 'expected 51.00 -> 50.00' in text
    assert 'Dry-run only; no rows changed.' in text
    history['register'].refresh_from_db()
    assert history['register'].current_balance == 1000
    for row, expected in zip(history['rows'], ('195.00', '51.00')):
        row.refresh_from_db()
        assert row.expected_amount == Decimal(expected)
    assert AuditLog.objects.filter(
        action=AuditLog.Action.FINANCIAL_REPAIR,
    ).count() == 0


def test_local_apply_is_exact_audited_and_idempotent(tmp_path, settings):
    from decimal import Decimal
    from base.models import AuditLog, SyncQueueRecord

    history = _history(tmp_path, settings)
    settings.SYNC_ENABLED = True
    _stamp_settlement_rows_as_synced(history['rows'])
    sync_before = {
        row.pk: {
            'sync_version': row.sync_version,
            'synced_at': row.synced_at,
            'updated_at': row.updated_at,
        }
        for row in history['rows']
    }
    SyncQueueRecord.objects.filter(
        model_name='shiftpaymenttotal',
    ).delete()
    _text, fingerprint = _run(history['plan_path'])

    text, _ = _run(
        history['plan_path'],
        apply=True,
        expect_fingerprint=fingerprint,
        confirm_repair_id=history['plan']['repair_id'],
    )

    assert '3 component write(s)' in text
    history['register'].refresh_from_db()
    assert history['register'].current_balance == Decimal('850.00')
    assert (
        history['register'].remote_cash_out_applied_total
        == Decimal('7.00')
    )
    for row, expected in zip(history['rows'], ('100.00', '50.00')):
        row.refresh_from_db()
        assert row.expected_amount == Decimal(expected)
        assert row.difference == Decimal('0.00')
        assert row.sync_version == sync_before[row.pk]['sync_version']
        assert row.synced_at == sync_before[row.pk]['synced_at']
        assert row.updated_at == sync_before[row.pk]['updated_at']
    assert not SyncQueueRecord.objects.filter(
        model_name='shiftpaymenttotal',
    ).exists()
    assert SyncQueueRecord.objects.filter(
        model_name='cashregister',
        record_uuid=history['register'].uuid,
    ).exists()

    audits = AuditLog.objects.filter(
        action=AuditLog.Action.FINANCIAL_REPAIR,
        branch_id='branch1',
    )
    assert audits.count() == 3
    register_audit = audits.get(metadata__component='register')
    assert register_audit.metadata['balance_before'] == '1000.00'
    assert register_audit.metadata['balance_after'] == '850.00'
    assert register_audit.metadata['expense_count'] == 2
    assert len(register_audit.metadata['expenses']) == 2
    assert register_audit.metadata['register_uuid'] == str(
        history['register'].uuid
    )
    assert register_audit.metadata['operator'] == history['plan']['operator']
    assert audits.filter(
        metadata__component='shift_payment_total',
    ).count() == 2
    assert SyncQueueRecord.objects.filter(
        model_name='auditlog',
        record_uuid__in=audits.values_list('uuid', flat=True),
    ).count() == 3

    # A fresh dry-run recognizes the append-only markers. Applying that exact
    # reviewed state again is a no-op, not a second register deduction.
    rerun_text, rerun_fingerprint = _run(history['plan_path'])
    assert 'Register repair: already applied' in rerun_text
    idempotent_text, _ = _run(
        history['plan_path'],
        apply=True,
        expect_fingerprint=rerun_fingerprint,
        confirm_repair_id=history['plan']['repair_id'],
    )
    assert '0 component write(s)' in idempotent_text
    history['register'].refresh_from_db()
    assert history['register'].current_balance == Decimal('850.00')
    assert audits.count() == 3


def test_stale_fingerprint_aborts_all_components_atomically(tmp_path, settings):
    from decimal import Decimal
    from base.models import AuditLog

    history = _history(tmp_path, settings)
    _text, fingerprint = _run(history['plan_path'])
    type(history['register']).objects.filter(pk=history['register'].pk).update(
        current_balance='1001.00',
    )

    with pytest.raises(CommandError, match='Evidence fingerprint changed'):
        _run(
            history['plan_path'],
            apply=True,
            expect_fingerprint=fingerprint,
            confirm_repair_id=history['plan']['repair_id'],
        )

    history['register'].refresh_from_db()
    assert history['register'].current_balance == Decimal('1001.00')
    for row, expected in zip(history['rows'], ('195.00', '51.00')):
        row.refresh_from_db()
        assert row.expected_amount == Decimal(expected)
    assert AuditLog.objects.filter(
        action=AuditLog.Action.FINANCIAL_REPAIR,
    ).count() == 0


def test_reconciled_shift_fails_closed_before_register_write(tmp_path, settings):
    from decimal import Decimal
    from base.models import AuditLog, CashReconciliation

    history = _history(tmp_path, settings)
    CashReconciliation.objects.create(
        shift=history['shifts'][0],
        expected_cash='100.00',
        actual_cash='100.00',
        difference='0.00',
        branch_id='branch1',
    )

    with pytest.raises(CommandError, match='manager reconciliation'):
        _run(history['plan_path'])

    history['register'].refresh_from_db()
    assert history['register'].current_balance == Decimal('1000.00')
    assert AuditLog.objects.filter(
        action=AuditLog.Action.FINANCIAL_REPAIR,
    ).count() == 0


def test_canonical_cash_mismatch_refuses_reviewed_plan(tmp_path, settings):
    history = _history(tmp_path, settings)
    plan = history['plan']
    repair = plan['shift_payment_total_repairs'][0]
    repair.update({
        'expected_after': '99.00',
        'difference_after': '1.00',
        'customer_change_excluded': '96.00',
    })
    history['plan_path'].write_text(json.dumps(plan), encoding='utf-8')

    with pytest.raises(CommandError, match='Canonical CASH.*not reviewed'):
        _run(history['plan_path'])


def test_cloud_apply_repairs_only_cloud_settlement_copies(tmp_path, settings):
    from decimal import Decimal
    from base.models import AuditLog, SyncQueueRecord
    from cashbox.models import ShiftPaymentTotal

    history = _history(tmp_path, settings)
    settings.SYNC_ENABLED = True
    _local_text, local_fingerprint = _run(history['plan_path'])
    _run(
        history['plan_path'],
        apply=True,
        expect_fingerprint=local_fingerprint,
        confirm_repair_id=history['plan']['repair_id'],
    )
    local_audits = AuditLog.objects.filter(
        action=AuditLog.Action.FINANCIAL_REPAIR,
        metadata__deployment_mode='local',
    )
    local_audits.update(synced_at=timezone.now())
    type(history['register'])._base_manager.filter(
        pk=history['register'].pk,
    ).update(synced_at=timezone.now())
    history['register'].refresh_from_db()

    # The test uses one database for both authorities. Restore only the cloud
    # copies of append-only settlement rows to their reviewed pre-repair state;
    # retain the synchronized local audit chain that authorizes cloud repair.
    for row, spec in zip(
        history['rows'],
        history['plan']['shift_payment_total_repairs'],
    ):
        ShiftPaymentTotal._base_manager.filter(pk=row.pk).update(
            expected_amount=spec['expected_before'],
            difference=spec['difference_before'],
        )
        row.refresh_from_db()

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    sync_before = {
        row.pk: {
            'sync_version': row.sync_version,
            'synced_at': row.synced_at,
            'updated_at': row.updated_at,
        }
        for row in history['rows']
    }
    SyncQueueRecord.objects.filter(
        model_name='shiftpaymenttotal',
    ).delete()
    text, fingerprint = _run(history['plan_path'])
    assert 'Register repair: skipped on cloud' in text

    applied, _ = _run(
        history['plan_path'],
        apply=True,
        expect_fingerprint=fingerprint,
        confirm_repair_id=history['plan']['repair_id'],
    )

    assert '2 component write(s)' in applied
    history['register'].refresh_from_db()
    assert history['register'].current_balance == Decimal('850.00')
    for row, expected in zip(history['rows'], ('100.00', '50.00')):
        row.refresh_from_db()
        assert row.expected_amount == Decimal(expected)
        assert row.sync_version == sync_before[row.pk]['sync_version']
        assert row.synced_at == sync_before[row.pk]['synced_at']
        assert row.updated_at == sync_before[row.pk]['updated_at']
    assert not SyncQueueRecord.objects.filter(
        model_name='shiftpaymenttotal',
    ).exists()
    audits = AuditLog.objects.filter(
        action=AuditLog.Action.FINANCIAL_REPAIR,
        metadata__deployment_mode='cloud',
    )
    assert audits.count() == 2
    assert not audits.filter(metadata__component='register').exists()


def test_cloud_apply_refuses_without_synchronized_local_predecessor(
    tmp_path, settings,
):
    history = _history(tmp_path, settings)
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'

    _text, fingerprint = _run(history['plan_path'])
    with pytest.raises(CommandError, match='synchronized local'):
        _run(
            history['plan_path'],
            apply=True,
            expect_fingerprint=fingerprint,
            confirm_repair_id=history['plan']['repair_id'],
        )


def test_cloud_apply_refuses_when_register_marker_outruns_register_sync(
    tmp_path, settings,
):
    from base.models import AuditLog
    from cashbox.models import ShiftPaymentTotal

    history = _history(tmp_path, settings)
    settings.SYNC_ENABLED = True
    _text, local_fingerprint = _run(history['plan_path'])
    _run(
        history['plan_path'],
        apply=True,
        expect_fingerprint=local_fingerprint,
        confirm_repair_id=history['plan']['repair_id'],
    )
    AuditLog.objects.filter(
        action=AuditLog.Action.FINANCIAL_REPAIR,
        metadata__deployment_mode='local',
    ).update(synced_at=timezone.now())
    # Simulate the exact dangerous ordering: all audit markers reached cloud,
    # but its CashRegister payload was rejected and remains at balance_before.
    type(history['register'])._base_manager.filter(
        pk=history['register'].pk,
    ).update(current_balance='1000.00', synced_at=timezone.now())
    for row, spec in zip(
        history['rows'],
        history['plan']['shift_payment_total_repairs'],
    ):
        ShiftPaymentTotal._base_manager.filter(pk=row.pk).update(
            expected_amount=spec['expected_before'],
            difference=spec['difference_before'],
        )

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    _text, fingerprint = _run(history['plan_path'])
    with pytest.raises(CommandError, match='CashRegister.*exact corrected'):
        _run(
            history['plan_path'],
            apply=True,
            expect_fingerprint=fingerprint,
            confirm_repair_id=history['plan']['repair_id'],
        )


def test_apply_requires_explicit_confirmation_and_fingerprint(tmp_path, settings):
    history = _history(tmp_path, settings)

    with pytest.raises(CommandError, match='confirm-repair-id'):
        _run(
            history['plan_path'],
            apply=True,
            expect_fingerprint='0' * 64,
        )
    with pytest.raises(CommandError, match='64-character'):
        _run(
            history['plan_path'],
            apply=True,
            expect_fingerprint='bad',
            confirm_repair_id=history['plan']['repair_id'],
        )


def test_plan_rejects_floating_money_and_duplicate_expense_ids(
    tmp_path, settings,
):
    history = _history(tmp_path, settings)
    plan = history['plan']
    plan['register_repair']['expenses'][0]['amount'] = 100.0
    history['plan_path'].write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(CommandError, match='decimal string'):
        _run(history['plan_path'])

    plan = history['plan']
    plan['register_repair']['expenses'][0]['amount'] = '100.00'
    plan['register_repair']['expenses'][1]['uuid'] = (
        plan['register_repair']['expenses'][0]['uuid']
    )
    history['plan_path'].write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(CommandError, match='duplicated'):
        _run(history['plan_path'])


def test_expense_cannot_be_consumed_by_a_different_repair_id(
    tmp_path, settings,
):
    from base.models import AuditLog

    history = _history(tmp_path, settings)
    first_expense = history['plan']['register_repair']['expenses'][0]
    AuditLog.objects.create(
        action=AuditLog.Action.FINANCIAL_REPAIR,
        target_type='CashRegister',
        target_id=history['register'].pk,
        branch_id='branch1',
        metadata={
            'repair_id': 'some-earlier-repair',
            'deployment_mode': 'local',
            'component': 'register',
            'expenses': [first_expense],
        },
    )

    with pytest.raises(CommandError, match='already consumed'):
        _run(history['plan_path'])


def test_register_uuid_and_marker_are_bound_to_same_live_row(
    tmp_path, settings,
):
    from base.models import CashRegister

    history = _history(tmp_path, settings)
    settings.SYNC_ENABLED = True
    _text, fingerprint = _run(history['plan_path'])
    _run(
        history['plan_path'],
        apply=True,
        expect_fingerprint=fingerprint,
        confirm_repair_id=history['plan']['repair_id'],
    )
    CashRegister._base_manager.filter(pk=history['register'].pk).update(
        is_deleted=True,
    )
    replacement = CashRegister.objects.create(
        branch_id='branch1',
        current_balance='850.00',
    )

    with pytest.raises(CommandError, match='does not match.*register UUID'):
        _run(history['plan_path'])
    assert str(replacement.uuid) != history['plan']['register_repair']['register_uuid']


def test_expense_must_match_exact_closed_shift_and_legacy_cutoff(
    tmp_path, settings,
):
    from base.models import Shift

    history = _history(tmp_path, settings)
    expense = history['expenses'][0]
    Shift._base_manager.filter(pk=expense.shift_id).update(
        status=Shift.Status.ACTIVE,
        end_time=None,
    )

    with pytest.raises(CommandError, match='reviewed closed branch shift'):
        _run(history['plan_path'])


def test_expense_created_at_must_match_plan_exactly(tmp_path, settings):
    from datetime import timedelta
    from cashbox.models import CashboxExpense

    history = _history(tmp_path, settings)
    expense = history['expenses'][0]
    CashboxExpense._base_manager.filter(pk=expense.pk).update(
        created_at=expense.created_at + timedelta(seconds=1),
    )

    with pytest.raises(CommandError, match='created_at no longer matches'):
        _run(history['plan_path'])


def test_shift_repair_requires_fully_synchronized_append_only_row(
    tmp_path, settings,
):
    from cashbox.models import ShiftPaymentTotal

    history = _history(tmp_path, settings)
    row = history['rows'][0]
    ShiftPaymentTotal._base_manager.filter(pk=row.pk).update(synced_at=None)

    with pytest.raises(CommandError, match='original synchronization'):
        _run(history['plan_path'])


def test_shift_repair_refuses_existing_outbound_queue_slot(tmp_path, settings):
    from base.models import SyncQueueRecord

    history = _history(tmp_path, settings)
    row = history['rows'][0]
    SyncQueueRecord.objects.create(
        model_name='shiftpaymenttotal',
        record_uuid=row.uuid,
        payload={'uuid': str(row.uuid), 'sync_version': row.sync_version},
    )

    with pytest.raises(CommandError, match='outbound sync queue slot'):
        _run(history['plan_path'])


def test_customer_change_claim_must_match_raw_tender_evidence(
    tmp_path, settings,
):
    from base.models import OrderPayment

    history = _history(tmp_path, settings)
    payment = OrderPayment.objects.filter(
        order__cashier=history['shifts'][0].user,
    ).first()
    OrderPayment._base_manager.filter(pk=payment.pk).update(amount='196.00')

    with pytest.raises(CommandError, match='Raw tender evidence'):
        _run(history['plan_path'])


def test_apply_does_not_print_success_before_atomic_exit(
    tmp_path, settings, monkeypatch,
):
    from django.db import transaction
    from base.management.commands import repair_financial_history as command_module

    history = _history(tmp_path, settings)
    settings.SYNC_ENABLED = True
    _text, fingerprint = _run(history['plan_path'])
    real_atomic = transaction.atomic

    class FailAtCommitBoundary:
        def __enter__(self):
            self._context = real_atomic()
            return self._context.__enter__()

        def __exit__(self, exc_type, exc_value, traceback):
            self._context.__exit__(exc_type, exc_value, traceback)
            raise RuntimeError('simulated commit-boundary failure')

    monkeypatch.setattr(
        command_module.Command,
        '_atomic',
        lambda self: FailAtCommitBoundary(),
    )
    out = StringIO()
    with pytest.raises(RuntimeError, match='commit-boundary failure'):
        call_command(
            'repair_financial_history',
            plan=str(history['plan_path']),
            branch='branch1',
            operator=history['plan']['operator'],
            apply=True,
            expect_fingerprint=fingerprint,
            confirm_repair_id=history['plan']['repair_id'],
            stdout=out,
        )
    assert 'Applied repair' not in out.getvalue()
