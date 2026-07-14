from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _paid_order(*, amount='100.00', method='PAYME', branch='branch-a'):
    from base.models import Order, OrderPayment, User

    user = User.objects.create(
        email=f'provider-refund-{uuid4().hex}@test.local',
        first_name='Provider',
        last_name='Customer',
        role='CASHIER',
        status='ACTIVE',
        password='!',
        branch_id=branch,
    )
    paid_at = timezone.now() - timedelta(hours=1)
    order = Order.objects.create(
        user=user,
        cashier=user,
        status='COMPLETED',
        is_paid=True,
        payment_method=method,
        paid_at=paid_at,
        subtotal=amount,
        total_amount=amount,
        branch_id=branch,
    )
    payment = OrderPayment.objects.create(
        order=order,
        method=method,
        amount=amount,
        branch_id=branch,
    )
    return order, payment


def test_multiple_provider_refunds_are_idempotent_append_only_events():
    from base.models import CashRegister, OrderRefund
    from base.services.order_refund import (
        SettlementInvariantError,
        record_external_provider_refund,
        refund_totals,
    )

    order, payment = _paid_order()
    original_paid_at = order.paid_at

    first, created = record_external_provider_refund(
        order, method='PAYME', amount='40.00', source_id='provider-event-1',
    )
    replay, replay_created = record_external_provider_refund(
        order, method='PAYME', amount='40.00', source_id='provider-event-1',
    )
    second, second_created = record_external_provider_refund(
        order, method='PAYME', amount='60.00', source_id='provider-event-2',
    )

    assert created is True and second_created is True
    assert replay_created is False and replay.pk == first.pk
    assert first.shift_id is None and first.cashier_id is None
    assert first.drawer_cash_amount == Decimal('0.00')
    assert second.drawer_cash_amount == Decimal('0.00')
    assert OrderRefund.objects.filter(order=order).count() == 2
    assert refund_totals(OrderRefund.objects.filter(order=order))['amount'] == Decimal('100.00')
    assert not CashRegister.objects.filter(branch_id=order.branch_id).exists()

    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_method == 'PAYME'
    assert order.paid_at == original_paid_at
    assert payment.is_deleted is False and payment.amount == Decimal('100.00')

    with pytest.raises(SettlementInvariantError, match='exceeds unreversed'):
        record_external_provider_refund(
            order, method='PAYME', amount='1.00', source_id='provider-event-3',
        )
    first.reason = 'rewrite history'
    with pytest.raises(TypeError, match='append-only'):
        first.save(update_fields=['reason'])


def test_product_reporting_allocates_partial_money_without_inventing_returned_units():
    from base.models import Category, OrderItem, Product
    from base.repositories.order_item import OrderItemRepository
    from base.services.order_refund import record_external_provider_refund

    order, _payment = _paid_order()
    category = Category.objects.create(
        name=f'Refund category {uuid4().hex}',
        slug=f'refund-category-{uuid4().hex}',
    )
    product = Product.objects.create(
        name=f'Refund product {uuid4().hex}',
        category=category,
        price='50.00',
        branch_id=order.branch_id,
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        quantity=2,
        price='50.00',
        original_price='50.00',
        branch_id=order.branch_id,
    )
    record_external_provider_refund(
        order, method='PAYME', amount='40.00', source_id='partial-product-refund',
    )

    rows = OrderItemRepository.get_top_products(
        date_from=order.paid_at - timedelta(minutes=1),
        date_to=timezone.now() + timedelta(minutes=1),
    )
    row = next(item for item in rows if item['product_id'] == product.id)

    assert row['gross_qty'] == 2
    assert row['refund_qty'] == 0
    assert row['total_qty'] == 2
    assert row['gross_revenue'] == Decimal('100.00')
    assert row['refund_revenue'] == Decimal('40.00')
    assert row['total_revenue'] == Decimal('60.00')


def test_provider_partial_then_cancel_reverses_units_and_order_count_once():
    from base.models import Category, OrderItem, OrderRefund, Product
    from base.repositories.order_item import OrderItemRepository
    from base.services.order_refund import record_external_provider_refund

    order, _payment = _paid_order()
    category = Category.objects.create(
        name=f'Combined refund category {uuid4().hex}',
        slug=f'combined-refund-category-{uuid4().hex}',
    )
    product = Product.objects.create(
        name=f'Combined refund product {uuid4().hex}',
        category=category,
        price='50.00',
        branch_id=order.branch_id,
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        quantity=2,
        price='50.00',
        original_price='50.00',
        branch_id=order.branch_id,
    )
    record_external_provider_refund(
        order,
        method='PAYME',
        amount='40.00',
        source_id='combined-provider-partial',
    )
    OrderRefund.objects.create(
        order=order,
        amount='60.00',
        cash_amount='0',
        drawer_cash_amount='0',
        card_amount='0',
        payme_amount='60.00',
        unknown_amount='0',
        refunded_at=timezone.now(),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=f'order-cancel:{order.uuid}',
        branch_id=order.branch_id,
    )

    rows = OrderItemRepository.get_top_products(
        date_from=order.paid_at - timedelta(minutes=1),
        date_to=timezone.now() + timedelta(minutes=1),
    )
    row = next(item for item in rows if item['product_id'] == product.id)

    assert row['gross_qty'] == 2
    assert row['refund_qty'] == 2
    assert row['total_qty'] == 0
    assert row['gross_revenue'] == Decimal('100.00')
    assert row['refund_revenue'] == Decimal('100.00')
    assert row['total_revenue'] == Decimal('0.00')
    assert row['gross_order_count'] == 1
    assert row['refund_order_count'] == 1
    assert row['order_count'] == 0
