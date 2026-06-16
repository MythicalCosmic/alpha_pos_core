"""is_staff_only discount gate + serialization round-trip.

A discount flagged is_staff_only must apply ONLY to an order whose linked
base.Customer is is_staff=True. Walk-in orders (no customer) and orders for a
non-staff customer are rejected; the flag must also survive create/serialize.
"""
import pytest
from decimal import Decimal

from base.models import Customer, Order, OrderItem
from discounts.models import Discount, DiscountType
from discounts.services import DiscountService


@pytest.fixture
def staff_discount(db):
    dt = DiscountType.objects.create(
        name='Staff 50', code='staff50', discount_method=DiscountType.Method.PERCENTAGE,
    )
    return Discount.objects.create(
        discount_type=dt, name='Staff Half Off', code='STAFF50',
        value=Decimal('50'), is_staff_only=True,
    )


def _order_with(product, regular_user, customer=None):
    order = Order.objects.create(
        user=regular_user, customer=customer, order_type='HALL',
        status='PREPARING', is_paid=False,
        display_id=Order.objects.count() + 1,
        subtotal='10.00', total_amount='10.00',
    )
    OrderItem.objects.create(order=order, product=product, quantity=1, price=product.price)
    return order


@pytest.mark.django_db
def test_staff_only_rejects_walk_in(staff_discount, product, regular_user):
    order = _order_with(product, regular_user, customer=None)
    result, status = DiscountService.apply_to_order(order.id, 'STAFF50', regular_user.id)
    assert not result['success']
    assert 'staff' in result['message'].lower()


@pytest.mark.django_db
def test_staff_only_rejects_non_staff_customer(staff_discount, product, regular_user):
    cust = Customer.objects.create(name='Walk In', phone_number='111', is_staff=False)
    order = _order_with(product, regular_user, customer=cust)
    result, status = DiscountService.apply_to_order(order.id, 'STAFF50', regular_user.id)
    assert not result['success']
    assert 'staff' in result['message'].lower()


@pytest.mark.django_db
def test_staff_only_allows_staff_customer(staff_discount, product, regular_user):
    cust = Customer.objects.create(name='Employee', phone_number='222', is_staff=True)
    order = _order_with(product, regular_user, customer=cust)
    result, status = DiscountService.apply_to_order(order.id, 'STAFF50', regular_user.id)
    assert result['success'], result
    order.refresh_from_db()
    assert order.discount_amount == Decimal('5.00')   # 50% of 10.00


@pytest.mark.django_db
def test_is_staff_only_serialized(staff_discount):
    from discounts.services.discount_service import _serialize_discount
    assert _serialize_discount(staff_discount)['is_staff_only'] is True
