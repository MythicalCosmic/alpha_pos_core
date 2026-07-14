from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _cashier_and_shift(*, branch='branch-a'):
    from base.models import Shift, User

    cashier = User.objects.create(
        email=f'refund-{uuid4().hex}@test.local',
        first_name='Refund',
        last_name='Cashier',
        password='!',
        role='CASHIER',
        status='ACTIVE',
        branch_id=branch,
    )
    shift = Shift.objects.create(
        user=cashier,
        start_time=timezone.now() - timedelta(hours=1),
        status=Shift.Status.ACTIVE,
        branch_id=branch,
    )
    return cashier, shift


def _paid_cash_order(cashier, *, amount='100.00', branch='branch-a'):
    from base.models import Order, OrderPayment

    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        status='COMPLETED',
        is_paid=True,
        payment_method='CASH',
        paid_at=timezone.now(),
        subtotal=amount,
        total_amount=amount,
        branch_id=branch,
    )
    OrderPayment.objects.create(
        order=order,
        method='CASH',
        amount=amount,
        branch_id=branch,
    )
    return order


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a')
def test_local_cash_refund_cannot_make_register_negative():
    from base.models import CashRegister, OrderRefund
    from base.services.order_refund import (
        SettlementInvariantError,
        record_paid_order_refund,
    )

    cashier, _shift = _cashier_and_shift()
    order = _paid_cash_order(cashier)
    register = CashRegister.objects.get(
        branch_id='branch-a', is_deleted=False,
    )
    register.current_balance = Decimal('40.00')
    register.save(update_fields=['current_balance'])

    with pytest.raises(SettlementInvariantError, match='available cash'):
        record_paid_order_refund(order, cashier.id, reason='customer return')

    register.refresh_from_db()
    assert register.current_balance == Decimal('40.00')
    assert not OrderRefund.objects.filter(order=order).exists()


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_cloud_cash_refund_reserves_against_other_pending_commands():
    from base.models import CashRegister, Inkassa, OrderRefund
    from base.services.order_refund import (
        SettlementInvariantError,
        record_paid_order_refund,
    )

    cashier, _shift = _cashier_and_shift()
    order = _paid_cash_order(cashier, amount='50.00')
    register = CashRegister.objects.get(
        branch_id='branch-a', is_deleted=False,
    )
    register.current_balance = Decimal('100.00')
    register.save(update_fields=['current_balance'])
    Inkassa.objects.create(
        cashier=cashier,
        amount='60.00',
        inkass_type=Inkassa.InkassType.CASH,
        balance_before='100.00',
        balance_after='40.00',
        register_command=True,
        notes=Inkassa.command_notes('already pending'),
        branch_id='branch-a',
    )

    assert Inkassa.pending_register_amount(register) == Decimal('60.00')
    with pytest.raises(SettlementInvariantError, match='available cash'):
        record_paid_order_refund(order, cashier.id, reason='customer return')

    register.refresh_from_db()
    assert register.current_balance == Decimal('100.00')
    assert Inkassa.pending_register_amount(register) == Decimal('60.00')
    assert not OrderRefund.objects.filter(order=order).exists()


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_cloud_refund_uses_one_legacy_compatible_cash_command():
    from base.models import CashRegister, Inkassa, OrderRefund
    from base.services.order_refund import record_paid_order_refund

    cashier, _shift = _cashier_and_shift()
    order = _paid_cash_order(cashier, amount='50.00')
    register = CashRegister.objects.get(
        branch_id='branch-a', is_deleted=False,
    )
    register.current_balance = Decimal('100.00')
    register.save(update_fields=['current_balance'])

    refund, created = record_paid_order_refund(
        order, cashier.id, reason='customer return',
    )

    assert created is True
    assert refund.register_command is False
    assert OrderRefund.visible_reason(refund.reason) == 'customer return'
    command = Inkassa.objects.get(
        branch_id='branch-a', register_command=True,
    )
    assert command.amount == Decimal('50.00')
    assert command.notes.startswith(Inkassa.refund_command_prefix())
    assert str(refund.uuid) in command.notes
    assert str(order.uuid) in command.notes
    assert Inkassa.pending_register_amount(register) == Decimal('50.00')

    # The refund row itself is accounting evidence, never a second drawer
    # command. Applying all pending commands therefore debits exactly once.
    with override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a'):
        assert Inkassa._apply_pending_register_commands('branch-a') is True
    register.refresh_from_db()
    assert register.current_balance == Decimal('50.00')
    assert register.remote_cash_out_applied_total == Decimal('50.00')
    assert Inkassa.pending_register_amount(register) == Decimal('0.00')


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a')
def test_refund_command_notes_bridge_pre_flag_desktop_pull():
    from base.models import CashRegister, Inkassa

    register = CashRegister.objects.create(
        branch_id='branch-a', current_balance='100.00',
    )
    payload = {
        'uuid': str(uuid4()),
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': 'branch-a',
        'amount': '30.00',
        'inkass_type': 'CASH',
        'balance_before': '100.00',
        'balance_after': '70.00',
        # Deliberately omit register_command: this is what a row retained by
        # the pre-column desktop looks like at upgrade time.
        'notes': Inkassa.command_notes(
            f'{Inkassa.REFUND_COMMAND_MARKER} legacy bridge',
        ),
    }

    command, action = Inkassa.from_sync_dict(payload, branch_id='branch-a')
    assert action == 'created'
    assert command.register_command is False
    register.refresh_from_db()
    assert register.current_balance == Decimal('70.00')
    assert register.remote_cash_out_applied_total == Decimal('30.00')


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a')
def test_remote_cash_command_stays_deferred_when_drawer_is_short():
    from base.models import CashRegister, Inkassa

    register = CashRegister.objects.create(
        branch_id='branch-a', current_balance='20.00',
    )
    payload = {
        'uuid': str(uuid4()),
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': 'branch-a',
        'amount': '30.00',
        'inkass_type': 'CASH',
        'balance_before': '30.00',
        'balance_after': '0.00',
        'register_command': True,
        'notes': Inkassa.command_notes('remote command'),
    }

    _command, action = Inkassa.from_sync_dict(payload, branch_id='branch-a')
    assert action == 'deferred'
    register.refresh_from_db()
    assert register.current_balance == Decimal('20.00')
    assert register.remote_cash_out_applied_total == Decimal('0.00')
    assert Inkassa.pending_register_amount(register) == Decimal('30.00')

    # A redelivery after physical cash arrives completes the same command once.
    register.current_balance = Decimal('40.00')
    register.save(update_fields=['current_balance'])
    _command, action = Inkassa.from_sync_dict(payload, branch_id='branch-a')
    # The row itself is an idempotent equal-version replay; the deferred cash
    # command side effect is what advances once funds become available.
    assert action == 'skipped'
    register.refresh_from_db()
    assert register.current_balance == Decimal('10.00')
    assert register.remote_cash_out_applied_total == Decimal('30.00')


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a')
def test_active_shift_branch_ownership_is_fail_closed():
    from base.models import Shift, User
    from base.services.order_refund import (
        SettlementInvariantError,
        lock_active_cashier_shift,
    )

    cashier, shift = _cashier_and_shift()
    # QuerySet.update deliberately bypasses SyncMixin's default branch fill so
    # this reproduces a damaged/legacy ownership record.
    User.objects.filter(pk=cashier.pk).update(branch_id='')
    Shift.objects.filter(pk=shift.pk).update(branch_id='')

    with pytest.raises(SettlementInvariantError, match='no branch ownership'):
        lock_active_cashier_shift(cashier.id, branch_id='branch-a')


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_global_cashier_provenance_does_not_override_shift_branch():
    from base.models import User
    from base.services.order_refund import lock_active_cashier_shift

    cashier, shift = _cashier_and_shift(branch='branch-a')
    # User is global sync identity.  Older cloud rows can retain the creating
    # node's provenance, but operational ownership always comes from Shift.
    User.objects.filter(pk=cashier.pk).update(branch_id='legacy-cloud-node')

    assert lock_active_cashier_shift(
        cashier.id, branch_id='branch-a',
    ).pk == shift.pk
