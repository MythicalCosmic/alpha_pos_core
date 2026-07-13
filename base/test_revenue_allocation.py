from decimal import Decimal

import pytest
from django.db.models import Sum


pytestmark = pytest.mark.django_db


def test_order_discount_is_allocated_proportionally_to_product_lines(
    regular_user, cashier_user,
):
    from base.models import Category, Order, OrderItem, Product
    from base.services.revenue import gross_line_revenue, net_line_revenue

    category = Category.objects.create(name='Food', slug='food')
    first = Product.objects.create(name='First', category=category, price=60)
    second = Product.objects.create(name='Second', category=category, price=40)
    order = Order.objects.create(
        user=regular_user, cashier=cashier_user, is_paid=True,
        subtotal=Decimal('100'), discount_amount=Decimal('10'),
        total_amount=Decimal('90'),
    )
    OrderItem.objects.create(order=order, product=first, quantity=1, price=60)
    OrderItem.objects.create(order=order, product=second, quantity=1, price=40)

    rows = {
        row['product_id']: row
        for row in OrderItem.objects.filter(order=order)
        .values('product_id')
        .annotate(gross=Sum(gross_line_revenue()), net=Sum(net_line_revenue()))
    }
    assert rows[first.id]['gross'] == Decimal('60')
    assert rows[first.id]['net'] == Decimal('54')
    assert rows[second.id]['net'] == Decimal('36')
    assert sum(row['net'] for row in rows.values()) == order.total_amount

    from base.repositories.order_item import OrderItemRepository
    ranked = OrderItemRepository.get_top_products(limit=10)
    by_product = {row['product_id']: row for row in ranked}
    assert by_product[first.id]['total_revenue'] == Decimal('54')
    assert by_product[second.id]['total_revenue'] == Decimal('36')
