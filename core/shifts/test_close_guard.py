"""end_shift's settlement guard.

Kitchen state and payment state are independent. Paid PREPARING/READY orders do
not block, while every non-cancelled unpaid order must be paid or cancelled before
the shift freezes its money totals.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from base.models import (
    ExternalOrderPayment, User, Order, OrderPayment, Shift,
)
from core.shifts.service import ShiftService


def _cashier(email):
    return User.objects.create(
        first_name='Ca', last_name='Sh', email=email,
        password='x', role='CASHIER', status='ACTIVE')


def _shift(u, *, eligible=True):
    return Shift.objects.create(
        user=u, start_time=timezone.now() - timedelta(hours=2), status='ACTIVE',
        treasury_settlement_eligible=eligible)


def _order(u, status, is_paid, *, with_payment=True):
    order = Order.objects.create(
        user=u, cashier=u, status=status, is_paid=is_paid, display_id=1,
        subtotal='10.00', total_amount='10.00',
        paid_at=(timezone.now() if is_paid else None),
        payment_method=('CASH' if is_paid else None))
    if is_paid and with_payment:
        OrderPayment.objects.create(order=order, method='CASH', amount='10.00')
    return order


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
def test_unpaid_kitchen_orders_block_close():
    u = _cashier('g2@x.com')
    s = _shift(u)
    _order(u, 'PREPARING', False)
    _order(u, 'READY', False)
    _, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 400
    s.refresh_from_db()
    assert s.status == 'ACTIVE'


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
def test_cancelled_unpaid_order_does_not_block_close():
    u = _cashier('g4-cancelled@x.com')
    s = _shift(u)
    _order(u, 'CANCELED', False)
    res, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 200, res


@pytest.mark.django_db
def test_eligible_close_rolls_back_on_settlement_failure(monkeypatch):
    """Never leave a new shift ENDED without a frozen settlement bundle."""
    import cashbox.services.drawer as drawer

    def boom(_shift):
        raise RuntimeError('no such table: cashbox_shiftpaymenttotal')

    monkeypatch.setattr(drawer, 'expected_payment_totals', boom)
    u = _cashier('g5@x.com')
    s = _shift(u)
    _order(u, 'READY', True)
    res, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 400, res
    s.refresh_from_db()
    assert s.status == 'ACTIVE'


@pytest.mark.django_db
def test_legacy_close_survives_settlement_failure(monkeypatch):
    """A pre-upgrade shift can still close when its optional settlement fails."""
    import cashbox.services.drawer as drawer

    def boom(_shift):
        raise RuntimeError('no such table: cashbox_shiftpaymenttotal')

    monkeypatch.setattr(drawer, 'expected_payment_totals', boom)
    u = _cashier('g5-legacy@x.com')
    s = _shift(u, eligible=False)
    _order(u, 'READY', True)
    res, st = ShiftService.end_shift(s.id, u.id, '')
    assert st == 200, res
    s.refresh_from_db()
    assert s.status == 'ENDED'


@pytest.mark.django_db
def test_eligible_close_blocks_paid_cash_header_without_payment_evidence():
    u = _cashier('missing-tender@x.com')
    s = _shift(u)
    order = _order(u, 'READY', True, with_payment=False)

    res, st = ShiftService.end_shift(s.id, u.id, '')

    assert st == 400, res
    assert str(order.id) in res['message']
    s.refresh_from_db()
    assert s.status == 'ACTIVE'


@pytest.mark.django_db
def test_eligible_close_blocks_paid_header_without_paid_at():
    u = _cashier('missing-paid-at@x.com')
    s = _shift(u)
    order = _order(u, 'READY', True)
    Order.objects.filter(pk=order.pk).update(paid_at=None)

    res, st = ShiftService.end_shift(s.id, u.id, '')

    assert st == 400, res
    assert str(order.id) in res['message']
    s.refresh_from_db()
    assert s.status == 'ACTIVE'


@pytest.mark.django_db
def test_external_courier_payment_closes_with_matching_v3_manifest():
    """Non-drawer evidence is frozen identically on branch and cloud."""
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import (
        _build_settlement_manifest, _settlement_bundle_error,
    )

    u = _cashier('external-courier@x.com')
    s = _shift(u)
    order = _order(u, 'READY', True, with_payment=False)
    order.order_type = Order.OrderType.DELIVERY
    order.save(update_fields=['order_type'])
    ExternalOrderPayment.objects.create(
        order=order,
        branch_id=order.branch_id,
        source=ExternalOrderPayment.Source.COURIER,
        source_id='courier-close-1',
        method=Order.PaymentMethod.CASH,
        amount=order.total_amount,
        occurred_at=order.paid_at,
    )

    res, st = ShiftService.end_shift(s.id, u.id, '')

    assert st == 200, res
    s.refresh_from_db()
    assert s.settlement_manifest['version'] == 3
    external = s.settlement_manifest['money_evidence'][
        'external_order_payments'
    ]
    assert external['count'] == 1
    rows = list(ShiftPaymentTotal.objects.filter(shift=s))
    assert _settlement_bundle_error(s, rows) is None
    # Courier cash never entered the cashier's physical register.
    assert next(row for row in rows if row.method == 'CASH').expected_amount == 0

    # Shifts closed by the immediately previous desktop remain verifiable: v2
    # did not identity-commit the edition-specific courier row, while canonical
    # tender recomputation now sees its synced external mirror.
    legacy_manifest = _build_settlement_manifest(s, rows, version=2)
    Shift.objects.filter(pk=s.pk).update(settlement_manifest=legacy_manifest)
    s.refresh_from_db()
    assert _settlement_bundle_error(s, rows) is None


@pytest.mark.django_db
def test_end_active_for_user_threads_counted_into_settlement():
    """The cashier's blind per-tender count posted to /shifts/end reaches
    end_shift, so the ShiftPaymentTotal reconciliation rows carry it."""
    from decimal import Decimal
    from cashbox.models import ShiftPaymentTotal
    u = _cashier('counted@x.com')
    s = _shift(u)
    res, st = ShiftService.end_active_for_user(
        u.id, notes='close', counted={'CASH': '100', 'UZCARD': '50'}, actor=u)
    assert st == 200, res
    assert res['data']['status'] == 'ENDED'
    rows = {r.method: r for r in ShiftPaymentTotal.objects.filter(shift_id=s.id)}
    assert rows['CASH'].counted_amount == Decimal('100')
    assert rows['UZCARD'].counted_amount == Decimal('50')
