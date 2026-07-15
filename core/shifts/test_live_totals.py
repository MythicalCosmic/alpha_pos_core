"""Live and frozen shift counters must use identical tender attribution."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def test_live_cash_includes_mixed_cash_leg_net_of_change():
    from base.models import Order, OrderPayment, Shift, User
    from core.shifts.service import ShiftService

    cashier = User.objects.create(
        email='mixed-live@test.local', first_name='Mix', last_name='Cashier',
        password='!', role='CASHIER', status='ACTIVE',
    )
    now = timezone.now()
    shift = Shift.objects.create(
        user=cashier, start_time=now - timedelta(hours=1), status='ACTIVE')
    order = Order.objects.create(
        user=cashier, cashier=cashier, status='COMPLETED', is_paid=True,
        paid_at=now, total_amount=Decimal('50000.00'), payment_method='MIXED')
    # Customer tendered 35k cash + 20k card for a 50k bill: 5k change means
    # only 30k physical cash entered the drawer.
    OrderPayment.objects.create(order=order, method='CASH', amount='35000.00')
    OrderPayment.objects.create(order=order, method='UZCARD', amount='20000.00')

    _, revenue, cash = ShiftService._live_totals(shift, now + timedelta(seconds=1))
    assert revenue == Decimal('50000.00')
    assert cash == Decimal('30000.00')


def test_shift_47_standalone_contract_uses_authoritative_tender_components():
    """Production shape: 140 paid rows, 9,993,000 total, no missing payments.

    The list path already used the canonical batch extras. The standalone
    detail/current serializer must expose the same 1,053,000 card and 70,000
    Payme instead of stale zero flat fields.
    """
    from base.models import Order, OrderPayment, Shift, User
    from core.shifts.service import ShiftService

    cashier = User.objects.create(
        email='shift-47@test.local', first_name='Shift', last_name='Forty Seven',
        password='!', role='CASHIER', status='ACTIVE', branch_id='branch1',
    )
    now = timezone.now()
    shift = Shift.objects.create(
        id=47,
        user=cashier,
        start_time=now - timedelta(hours=8),
        status='ACTIVE',
        branch_id='branch1',
    )
    amounts = (
        [('CASH', Decimal('70000.00'))] * 125
        + [('CASH', Decimal('120000.00'))]
        + [('HUMO', Decimal('80000.00'))] * 12
        + [('HUMO', Decimal('93000.00'))]
        + [('PAYME', Decimal('70000.00'))]
    )
    orders = Order.objects.bulk_create([
        Order(
            user=cashier,
            cashier=cashier,
            status='READY',
            is_paid=True,
            paid_at=now - timedelta(minutes=1),
            payment_method=method,
            subtotal=amount,
            total_amount=amount,
            branch_id='branch1',
        )
        for method, amount in amounts
    ])
    OrderPayment.objects.bulk_create([
        OrderPayment(
            order=order, method=method, amount=amount, branch_id='branch1',
        )
        for order, (method, amount) in zip(orders, amounts)
    ])

    response, status = ShiftService.get(shift.id, actor=cashier)

    assert status == 200, response
    row = response['data']
    assert row['paid_orders'] == 140
    assert row['total_revenue'] == '9993000.00'
    assert row['cash_collected'] == '8870000.00'
    assert row['card_collected'] == '1053000.00'
    assert row['payme_collected'] == '70000.00'
    assert row['payment_mix'] == {
        'cash': '8870000.00', 'card': '1053000.00', 'payme': '70000.00',
    }
