from datetime import timedelta

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_paid_and_refund_cursors_are_local_and_never_synchronized(
    regular_user, settings,
):
    from base.models import CashRegister, Order, OrderRefund

    settings.BRANCH_ID = 'branch-a'
    economic_time = timezone.now() - timedelta(days=2)
    before_order = timezone.now()
    order = Order.objects.create(
        user=regular_user,
        branch_id='branch-a',
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.PAYME,
        paid_at=economic_time,
        subtotal='100.00',
        total_amount='100.00',
    )

    assert order.accounting_recorded_at >= before_order
    assert order.accounting_recorded_at > order.paid_at
    assert 'accounting_recorded_at' not in order.to_sync_dict()
    assert CashRegister.objects.filter(
        branch_id='branch-a', is_deleted=False,
    ).count() == 1

    before_refund = timezone.now()
    refund = OrderRefund.objects.create(
        order=order,
        amount='40.00',
        cash_amount='0.00',
        drawer_cash_amount='0.00',
        card_amount='0.00',
        payme_amount='40.00',
        unknown_amount='0.00',
        refunded_at=economic_time + timedelta(hours=1),
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id='cursor-refund',
        branch_id='branch-a',
    )

    # SQLite's STRFTIME-backed database default has millisecond precision,
    # while Python's clock includes microseconds.  The cursor is still stamped
    # by the INSERT after the lock; allow only that sub-millisecond truncation.
    assert refund.accounting_recorded_at >= before_refund - timedelta(milliseconds=1)
    assert refund.accounting_recorded_at > refund.refunded_at
    assert 'accounting_recorded_at' not in refund.to_sync_dict()


@pytest.mark.django_db(transaction=True)
def test_branch_accounting_lock_requires_transaction(settings):
    from base.services.accounting_cursor import lock_branch_accounting

    settings.BRANCH_ID = 'branch-a'
    with pytest.raises(RuntimeError, match='requires an atomic transaction'):
        lock_branch_accounting('branch-a')
