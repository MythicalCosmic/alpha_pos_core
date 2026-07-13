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

