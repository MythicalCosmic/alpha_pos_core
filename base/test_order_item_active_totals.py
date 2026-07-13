from decimal import Decimal

import pytest


pytestmark = pytest.mark.django_db


def test_order_total_and_existing_line_ignore_soft_deleted_items(
    order_factory, product,
):
    from base.models import OrderItem
    from base.repositories.order_item import OrderItemRepository

    order = order_factory(items=0)
    live = OrderItem.objects.create(
        order=order, product=product, quantity=2, price=Decimal('10'),
    )
    deleted = OrderItem.objects.create(
        order=order, product=product, quantity=9, price=Decimal('10'),
    )
    deleted.delete()

    assert OrderItemRepository.get_existing_unready(
        order.id, product.id,
    ).id == live.id
    assert OrderItemRepository.calculate_order_total(order) == Decimal('20')
