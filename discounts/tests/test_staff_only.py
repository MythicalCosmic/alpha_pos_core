"""is_staff_only discount gate + serialization round-trip.

A discount flagged is_staff_only must apply ONLY to an order whose linked
base.Customer is is_staff=True. Walk-in orders (no customer) and orders for a
non-staff customer are rejected; the flag must also survive create/serialize.
"""
import pytest
from decimal import Decimal

from django.test import override_settings

from base.models import Customer, Order, OrderItem
from discounts.models import Discount, DiscountType, DiscountUsage, OrderDiscount
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


def _order_with(product, regular_user, customer=None, branch_id=None):
    order_kwargs = {}
    if branch_id is not None:
        order_kwargs['branch_id'] = branch_id
    order = Order.objects.create(
        user=regular_user, customer=customer, order_type='HALL',
        status='PREPARING', is_paid=False,
        display_id=Order.objects.count() + 1,
        subtotal='10.00', total_amount='10.00',
        **order_kwargs,
    )
    OrderItem.objects.create(
        order=order, product=product, quantity=1, price=product.price,
        branch_id=order.branch_id,
    )
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


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_discount_money_children_inherit_the_order_branch(
    staff_discount, product, regular_user,
):
    customer = Customer.objects.create(
        name='Branch Employee', phone_number='223', is_staff=True,
        branch_id='branch-a',
    )
    order = _order_with(
        product, regular_user, customer=customer, branch_id='branch-a',
    )

    result, status = DiscountService.apply_to_order(
        order.id, 'STAFF50', regular_user.id,
    )

    assert status == 200, result
    assert OrderDiscount.objects.get(order=order).branch_id == 'branch-a'
    assert DiscountUsage.objects.get(order=order).branch_id == 'branch-a'


@pytest.mark.django_db
def test_apply_and_remove_publish_usage_counter(
    staff_discount, product, regular_user,
):
    from django.utils import timezone

    cust = Customer.objects.create(name='Employee', phone_number='333', is_staff=True)
    order = _order_with(product, regular_user, customer=cust)
    Discount.objects.filter(pk=staff_discount.pk).update(synced_at=timezone.now())
    staff_discount.refresh_from_db()
    start_version = staff_discount.sync_version

    result, status = DiscountService.apply_to_order(
        order.id, 'STAFF50', regular_user.id,
    )
    assert status == 200, result
    staff_discount.refresh_from_db()
    assert staff_discount.usage_count == 1
    assert staff_discount.sync_version == start_version + 1
    assert staff_discount.synced_at is None

    result, status = DiscountService.remove_from_order(
        order.id, result['data']['order_discount_id'], regular_user.id,
    )
    assert status == 200, result
    staff_discount.refresh_from_db()
    assert staff_discount.usage_count == 0
    assert staff_discount.sync_version == start_version + 2
    assert staff_discount.synced_at is None


@pytest.mark.django_db
def test_reapplying_discount_ignores_soft_deleted_order_lines(
    staff_discount, product, regular_user,
):
    cust = Customer.objects.create(name='Employee', phone_number='444', is_staff=True)
    order = _order_with(product, regular_user, customer=cust)
    removed = OrderItem.objects.create(
        order=order, product=product, quantity=9, price=product.price,
    )
    removed.delete()

    first, status = DiscountService.apply_to_order(
        order.id, 'STAFF50', regular_user.id,
    )
    assert status == 200, first
    assert Decimal(first['data']['discount_amount']) == Decimal('5.00')

    removed_result, status = DiscountService.remove_from_order(
        order.id, first['data']['order_discount_id'], regular_user.id,
    )
    assert status == 200, removed_result

    reapplied, status = DiscountService.apply_to_order(
        order.id, 'STAFF50', regular_user.id,
    )
    assert status == 200, reapplied
    assert Decimal(reapplied['data']['discount_amount']) == Decimal('5.00')


@pytest.mark.django_db
def test_is_staff_only_serialized(staff_discount):
    from discounts.services.discount_service import _serialize_discount
    assert _serialize_discount(staff_discount)['is_staff_only'] is True
