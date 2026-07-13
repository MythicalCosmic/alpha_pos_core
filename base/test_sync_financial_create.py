"""Regression coverage for trusted cloud -> branch order creation."""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

pytestmark = pytest.mark.django_db


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='till-1')
def test_new_cloud_order_keeps_money_and_venue_but_updates_still_protect_money():
    from base.models import Order, Place, Table, User

    user = User.objects.create(
        email='cloud-cashier@test.local', first_name='Cloud', last_name='Cashier',
        password='!', role='CASHIER', branch_id='cloud',
    )
    place = Place.objects.create(name='Main hall', branch_id='cloud')
    table = Table.objects.create(place=place, number='7', branch_id='cloud')
    created = timezone.now() - timedelta(days=2)
    order_uuid = str(uuid.uuid4())
    payload = {
        'uuid': order_uuid,
        'sync_version': 4,
        'is_deleted': False,
        'branch_id': 'cloud',
        'user_uuid': str(user.uuid),
        'place_uuid': str(place.uuid),
        'table_uuid': str(table.uuid),
        'status': 'COMPLETED',
        'is_paid': True,
        'payment_method': 'PAYME',
        'subtotal': '125000.00',
        'discount_amount': '5000.00',
        'discount_percent': '4.00',
        'total_amount': '120000.00',
        'paid_at': timezone.now().isoformat(),
        'created_at': created.isoformat(),
    }

    order, action = Order.from_sync_dict(payload)
    assert action == 'created'
    order.refresh_from_db()
    assert order.total_amount == Decimal('120000.00')
    assert order.subtotal == Decimal('125000.00')
    assert order.discount_amount == Decimal('5000.00')
    assert order.discount_percent == Decimal('4.00')
    assert order.is_paid is True and order.payment_method == 'PAYME'
    assert order.place_id == place.id and order.table_id == table.id
    assert abs(order.created_at - created) < timedelta(seconds=1)

    # A later remote update may advance workflow state, but it must not rewrite
    # financials already owned by this till.
    update = dict(payload, sync_version=5, status='READY', total_amount='1.00',
                  subtotal='1.00', is_paid=False, payment_method='CASH')
    order, action = Order.from_sync_dict(update)
    assert action == 'updated'
    order.refresh_from_db()
    assert order.status == 'READY'
    assert order.total_amount == Decimal('120000.00')
    assert order.subtotal == Decimal('125000.00')
    assert order.is_paid is True and order.payment_method == 'PAYME'

