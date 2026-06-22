"""end_shift's open-order guard.

A till must always be closeable once its sales are settled. The guard therefore
blocks only on a genuinely in-progress sale — an UNPAID, still-OPEN cart. Paid
orders (cash already in) and kitchen-side orders (PREPARING/READY) carry over and
must not block, which is what previously left shifts open forever once paid orders
piled up in READY because the kitchen never marked them COMPLETED.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from base.models import User, Order, Shift
from core.shifts.service import ShiftService


def _cashier(email):
    return User.objects.create(
        first_name='Ca', last_name='Sh', email=email,
        password='x', role='CASHIER', status='ACTIVE')


def _shift(u):
    return Shift.objects.create(
        user=u, start_time=timezone.now() - timedelta(hours=2), status='ACTIVE')


def _order(u, status, is_paid):
    return Order.objects.create(
        user=u, cashier=u, status=status, is_paid=is_paid, display_id=1,
        subtotal='10.00', total_amount='10.00',
        paid_at=(timezone.now() if is_paid else None),
        payment_method=('CASH' if is_paid else None))


@pytest.mark.django_db
def test_paid_kitchen_orders_do_not_block_close():
    u = _cashier('g1@x.com')
    s = _shift(u)
    _order(u, 'READY', True)       # the real-world stuck case: paid, kitchen done
    _order(u, 'PREPARING', True)
    res, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 200, res
    assert res['data']['status'] == 'ENDED'


@pytest.mark.django_db
def test_unpaid_kitchen_orders_do_not_block_close():
    u = _cashier('g2@x.com')
    s = _shift(u)
    _order(u, 'PREPARING', False)  # dine-in not yet paid -> carries over
    _order(u, 'READY', False)
    res, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 200, res


@pytest.mark.django_db
def test_unpaid_open_cart_blocks_close():
    u = _cashier('g3@x.com')
    s = _shift(u)
    _order(u, 'OPEN', False)       # genuine mid-transaction unpaid cart
    _, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 400
    s.refresh_from_db()
    assert s.status == 'ACTIVE'    # refused


@pytest.mark.django_db
def test_paid_open_order_does_not_block_close():
    u = _cashier('g4@x.com')
    s = _shift(u)
    _order(u, 'OPEN', True)        # money already in -> not blocking
    res, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 200, res


@pytest.mark.django_db
def test_close_survives_settlement_failure(monkeypatch):
    """A settlement-write error (e.g. a missing cashbox table on a half-migrated
    DB) must NOT roll back the close — the shift is already ENDED before that
    best-effort block. This is the 'shift won't close at all' bug: the unguarded
    settlement block used to 500 and revert the ENDED write inside the atomic."""
    import cashbox.services.drawer as drawer

    def boom(_shift):
        raise RuntimeError('no such table: cashbox_shiftpaymenttotal')

    monkeypatch.setattr(drawer, 'expected_payment_totals', boom)
    u = _cashier('g5@x.com')
    s = _shift(u)
    _order(u, 'READY', True)
    res, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 200, res
    assert res['data']['status'] == 'ENDED'
    s.refresh_from_db()
    assert s.status == 'ENDED'
