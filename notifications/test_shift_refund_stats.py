from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from base.models import (
    Category, Order, OrderItem, OrderPayment, OrderRefund, Product, Shift, User,
)
from notifications.handlers.shift import ShiftNotification


pytestmark = pytest.mark.django_db


def test_shift_notification_uses_sale_and_refund_event_clocks():
    now = timezone.now()
    user = User.objects.create(
        first_name='Cash', last_name='One', email='shift-refund@example.test',
        password='!', role='CASHIER', status='ACTIVE', branch_id='main',
    )
    shift = Shift.objects.create(
        user=user, status=Shift.Status.ACTIVE,
        start_time=now - timedelta(hours=3), branch_id='main',
    )
    category = Category.objects.create(name='Meals', branch_id='main')
    product = Product.objects.create(
        category=category, name='Burger', price=Decimal('100.00'),
        branch_id='main',
    )
    order = Order.objects.create(
        user=user, cashier=user, order_type=Order.OrderType.HALL,
        status=Order.Status.CANCELED, is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        subtotal=Decimal('100.00'), total_amount=Decimal('100.00'),
        paid_at=now - timedelta(hours=2), branch_id='main',
    )
    Order.objects.filter(pk=order.pk).update(
        created_at=now - timedelta(hours=2),
    )
    OrderItem.objects.create(
        order=order, product=product, quantity=1, price=Decimal('100.00'),
        branch_id='main',
    )
    OrderPayment.objects.create(
        order=order, method=Order.PaymentMethod.CASH,
        amount=Decimal('100.00'), branch_id='main',
    )
    OrderRefund.objects.create(
        order=order, shift=shift, cashier=user,
        amount=Decimal('100.00'), cash_amount=Decimal('100.00'),
        drawer_cash_amount=Decimal('100.00'),
        refunded_at=now - timedelta(minutes=30), branch_id='main',
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=str(order.uuid),
    )

    stats = ShiftNotification._get_shift_stats(
        now - timedelta(hours=1), now, user.id,
    )

    # The sale belongs to its earlier paid-at window. This window contains only
    # the reversal, so net money and realized units are negative—not erased.
    assert stats['gross_revenue'] == Decimal('0')
    assert stats['refunds'] == Decimal('100.00')
    assert stats['total_revenue'] == Decimal('-100.00')
    assert stats['refunded_orders'] == 1
    assert stats['order_types']['HALL']['revenue'] == Decimal('-100.00')
    assert stats['top_products'][0]['qty'] == -1
    assert stats['top_products'][0]['rev'] == Decimal('-100.00')
