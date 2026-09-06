"""Exercise coupon eligibility at application time and large valid baskets."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import tracemalloc

import pytest
from django.utils import timezone

from base.repositories import OrderRepository
from discounts.models import Discount, DiscountType, DiscountUsage, OrderDiscount
from discounts.services import DiscountService


@pytest.fixture
def coupon(db):
    kind = DiscountType.objects.create(
        name='Ten percent', code='ten-percent',
        discount_method=DiscountType.Method.PERCENTAGE,
    )
    return Discount.objects.create(
        discount_type=kind, name='Ten percent', code='TEN', value=Decimal('10'),
    )


@pytest.mark.parametrize('minimum, accepted', [('5', True), ('10', True), ('11', False)])
def test_coupon_minimum_uses_actual_order_subtotal(coupon, order_factory, regular_user, minimum, accepted):
    coupon.min_order_amount = Decimal(minimum)
    coupon.save(update_fields=['min_order_amount'])
    order = order_factory()
    result, status = DiscountService.apply_to_order(order.id, coupon.code, regular_user.id)
    assert result['success'] is accepted, result
    order.refresh_from_db()
    coupon.refresh_from_db()
    assert order.total_amount == Decimal('9' if accepted else '10')
    assert coupon.usage_count == int(accepted)
    assert OrderDiscount.objects.filter(order=order).count() == int(accepted)


@pytest.mark.parametrize('changed', ['inactive', 'expired', 'not-started', 'deleted', 'used-up'])
def test_coupon_rechecks_current_state_after_order_lock(coupon, order_factory, regular_user, monkeypatch, changed):
    order = order_factory()
    original = OrderRepository.get_for_update

    def lock_order(order_id):
        updates = {
            'inactive': {'is_active': False},
            'expired': {'end_date': timezone.now() - timedelta(days=1)},
            'not-started': {'start_date': timezone.now() + timedelta(days=1)},
            'deleted': {'is_deleted': True},
            'used-up': {'usage_limit': 1, 'usage_count': 1},
        }[changed]
        # Deterministically place a state change in the old unlocked-read window.
        Discount.objects.filter(pk=coupon.pk).update(**updates)
        return original(order_id)

    monkeypatch.setattr(OrderRepository, 'get_for_update', lock_order)
    result, status = DiscountService.apply_to_order(order.id, coupon.code, regular_user.id)
    assert status in (400, 404), result
    assert not result['success']
    order.refresh_from_db()
    assert order.total_amount == Decimal('10')
    assert not OrderDiscount.objects.filter(order=order).exists()
    assert not DiscountUsage.objects.filter(order=order).exists()


def test_buy_get_large_quantity_uses_bounded_memory_and_cheapest_units():
    discount = Discount(
        discount_type=DiscountType(discount_method=DiscountType.Method.BUY_X_GET_Y),
        buy_quantity=2, get_quantity=1,
    )
    items = [
        SimpleNamespace(price=Decimal('3'), quantity=200_000),
        SimpleNamespace(price=Decimal('1'), quantity=50_000),
    ]
    tracemalloc.start()
    try:
        amount = DiscountService.calculate_discount(discount, items)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # 83,333 units free: all 50,000 cheap units, then 33,333 at 3 each.
    assert amount == Decimal('149999.00')
    # Two order lines should not allocate a separate list entry for every unit.
    assert peak < 1_000_000, f'Coupon calculation allocated {peak} bytes'


@pytest.mark.parametrize('quantities, expected', [([1, 1], '0'), ([2, 1], '1'), ([3, 3], '2')])
def test_buy_get_small_baskets(quantities, expected):
    discount = Discount(
        discount_type=DiscountType(discount_method=DiscountType.Method.BUY_X_GET_Y),
        buy_quantity=2, get_quantity=1,
    )
    items = [SimpleNamespace(price=price, quantity=quantity) for price, quantity in zip(
        (Decimal('3'), Decimal('1')), quantities,
    )]
    assert DiscountService.calculate_discount(discount, items) == Decimal(expected)
