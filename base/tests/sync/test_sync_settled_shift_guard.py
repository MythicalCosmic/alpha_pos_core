"""Late sync evidence must never rewrite a completed shift settlement."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _completed_shift(cashier, reconciler):
    from base.models import CashReconciliation, Shift, User

    branch = 'branch1'
    User.objects.filter(pk__in=[cashier.pk, reconciler.pk]).update(
        branch_id=branch,
    )
    cashier.refresh_from_db()
    reconciler.refresh_from_db()
    end = timezone.now()
    shift = Shift.objects.create(
        user=cashier,
        branch_id=branch,
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.COMPLETED,
    )
    CashReconciliation.objects.create(
        shift=shift,
        expected_cash='0.00',
        actual_cash='0.00',
        difference='0.00',
        reconciled_by=reconciler,
        branch_id=branch,
    )
    return shift


def _paid_order(shift):
    from base.models import Order

    action_id = uuid4()
    order = Order.objects.create(
        user=shift.user,
        cashier=shift.user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        payment_action_id=action_id,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=shift.end_time - timedelta(minutes=30),
    )
    return order, action_id


def _payment_payload(order, action_id, *, payment_uuid=None):
    return {
        'uuid': str(payment_uuid or uuid4()),
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': order.branch_id,
        'order_uuid': str(order.uuid),
        'method': 'CASH',
        'amount': '100.00',
        'payment_action_id': str(action_id),
        'line_index': 0,
        'created_at': timezone.now().isoformat(),
    }


def test_completed_shift_rejects_new_synced_payment_evidence(
    settings,
    cashier_user,
    admin_user,
):
    from base.models import OrderPayment
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    shift = _completed_shift(cashier_user, admin_user)
    order, action_id = _paid_order(shift)
    payload = _payment_payload(order, action_id)

    result = CloudReceiver.receive_batch(
        'orderpayment',
        shift.branch_id,
        [payload],
    )

    assert result['acknowledged_uuids'] == []
    assert result['rejected_uuids'] == [payload['uuid']]
    assert result['record_results'][0]['reason_code'] == (
        'SETTLED_SHIFT_EVIDENCE_REWRITE'
    )
    assert not OrderPayment.objects.filter(uuid=payload['uuid']).exists()


def test_completed_shift_acknowledges_exact_payment_replay(
    settings,
    cashier_user,
    admin_user,
):
    from base.models import OrderPayment
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    shift = _completed_shift(cashier_user, admin_user)
    order, action_id = _paid_order(shift)
    payment = OrderPayment.objects.create(
        order=order,
        method='CASH',
        amount='100.00',
        payment_action_id=action_id,
        line_index=0,
        branch_id=shift.branch_id,
    )
    payload = payment.to_sync_dict()

    result = CloudReceiver.receive_batch(
        'orderpayment',
        shift.branch_id,
        [payload],
    )

    assert result['acknowledged_uuids'] == [str(payment.uuid)]
    assert result['rejected_uuids'] == []
    assert result['record_results'][0]['reason_code'] == (
        'IDEMPOTENT_SETTLED_SHIFT_EVIDENCE_REPLAY'
    )
    assert OrderPayment.objects.filter(uuid=payment.uuid).count() == 1


def test_completed_shift_rejects_existing_cashbox_expense_rewrite(
    settings,
    cashier_user,
    admin_user,
):
    from base.services.sync.receiver import CloudReceiver
    from cashbox.models import CashboxExpense

    settings.DEPLOYMENT_MODE = 'cloud'
    shift = _completed_shift(cashier_user, admin_user)
    expense = CashboxExpense.objects.create(
        shift=shift,
        amount='10.00',
        comment='Frozen expense',
        created_by=cashier_user,
        branch_id=shift.branch_id,
    )
    payload = expense.to_sync_dict()
    payload.update({
        'sync_version': expense.sync_version + 1,
        'amount': '999.00',
    })

    result = CloudReceiver.receive_batch(
        'cashboxexpense',
        shift.branch_id,
        [payload],
    )

    assert result['acknowledged_uuids'] == []
    assert result['rejected_uuids'] == [str(expense.uuid)]
    assert result['record_results'][0]['reason_code'] == (
        'SETTLED_SHIFT_EVIDENCE_REWRITE'
    )
    expense.refresh_from_db()
    assert str(expense.amount) == '10.00'


def test_locked_order_recomputes_stale_incoming_shift_projection(
    settings,
    cashier_user,
):
    from base.models import Order, Shift, User
    from base.services.sync.receiver import (
        RetryableSyncError,
        _financial_owner_plan,
        _lock_financial_owners,
    )

    settings.DEPLOYMENT_MODE = 'cloud'
    User.objects.filter(pk=cashier_user.pk).update(branch_id='branch1')
    cashier_user.refresh_from_db()
    now = timezone.now()
    first = Shift.objects.create(
        user=cashier_user,
        branch_id='branch1',
        start_time=now - timedelta(hours=4),
        end_time=now - timedelta(hours=3),
        status=Shift.Status.ENDED,
    )
    second = Shift.objects.create(
        user=cashier_user,
        branch_id='branch1',
        start_time=now - timedelta(hours=2),
        end_time=now - timedelta(hours=1),
        status=Shift.Status.ENDED,
    )
    order = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id='branch1',
        display_id=10,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        subtotal='10.00',
        total_amount='10.00',
        paid_at=first.end_time - timedelta(minutes=30),
    )
    plan = _financial_owner_plan(
        Order,
        order.uuid,
        {'status': Order.Status.READY},
        {},
        'branch1',
        False,
    )

    # Model a concurrent receiver winning before this transaction gets the
    # Order lock. The stale plan covers first; the locked row now belongs to
    # second and must force a fresh Shift-first attempt.
    Order.objects.filter(pk=order.pk).update(
        paid_at=second.end_time - timedelta(minutes=30),
    )
    with pytest.raises(RetryableSyncError) as exc_info:
        _lock_financial_owners(plan, {})
    assert exc_info.value.reason_code == 'FINANCIAL_OWNER_CHANGED'


def test_reparented_child_is_retried_after_target_lock(
    settings,
    cashier_user,
    admin_user,
):
    from base.models import OrderPayment
    from base.services.sync.receiver import (
        RetryableSyncError,
        _financial_owner_plan,
        _lock_financial_owners,
        _verify_locked_financial_target,
    )

    settings.DEPLOYMENT_MODE = 'cloud'
    first_shift = _completed_shift(cashier_user, admin_user)
    first_order, action_id = _paid_order(first_shift)

    # A second completed window gives the raced child a different financial
    # owner without relying on overlapping-shift ambiguity.
    second_shift = _completed_shift(cashier_user, admin_user)
    second_shift.start_time = first_shift.start_time - timedelta(hours=2)
    second_shift.end_time = first_shift.start_time - timedelta(hours=1)
    second_shift.save(update_fields=['start_time', 'end_time'])
    second_order, _ = _paid_order(second_shift)

    payment = OrderPayment.objects.create(
        order=first_order,
        method='CASH',
        amount='100.00',
        payment_action_id=action_id,
        line_index=0,
        branch_id=first_shift.branch_id,
    )
    resolved_fks = {'order': first_order}
    plan = _financial_owner_plan(
        OrderPayment,
        payment.uuid,
        {'method': 'CASH', 'amount': payment.amount},
        resolved_fks,
        first_shift.branch_id,
        False,
    )
    OrderPayment.objects.filter(pk=payment.pk).update(order=second_order)
    guard = _lock_financial_owners(plan, resolved_fks)
    payment.refresh_from_db()

    with pytest.raises(RetryableSyncError) as exc_info:
        _verify_locked_financial_target(plan, guard, payment)
    assert exc_info.value.reason_code == 'FINANCIAL_OWNER_CHANGED'


def test_shiftless_provider_refund_exact_replay_remains_acknowledged(
    settings,
    cashier_user,
    admin_user,
):
    from base.models import OrderRefund
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    sale_shift = _completed_shift(cashier_user, admin_user)
    order, _ = _paid_order(sale_shift)
    refund = OrderRefund.objects.create(
        order=order,
        shift=None,
        cashier=None,
        branch_id=sale_shift.branch_id,
        amount='10.00',
        cash_amount='0.00',
        drawer_cash_amount='0.00',
        card_amount='10.00',
        payme_amount='0.00',
        unknown_amount='0.00',
        card_detail={'HUMO': '10.00'},
        refunded_at=timezone.now(),
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id='provider-refund-shiftless-replay',
        reason='Provider callback',
    )
    payload = refund.to_sync_dict()

    result = CloudReceiver.receive_batch(
        'orderrefund',
        sale_shift.branch_id,
        [payload],
    )

    assert result['acknowledged_uuids'] == [str(refund.uuid)]
    assert result['rejected_uuids'] == []
    assert result['retryable_uuids'] == []
    assert result['record_results'][0]['reason_code'] == (
        'IDEMPOTENT_APPEND_ONLY_REPLAY'
    )
