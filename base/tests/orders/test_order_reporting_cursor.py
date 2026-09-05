"""Order updates must remain discoverable by incremental report readers."""
from datetime import timedelta

import pytest
from django.utils import timezone

from base.models import Order, User


pytestmark = pytest.mark.django_db


@pytest.fixture
def cached_order(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'cursor-branch'
    user = User.objects.create(
        first_name='Cursor', last_name='Test', email='cursor@example.com',
        password='!', role=User.RoleChoices.CASHIER,
    )
    order = Order.objects.create(user=user, subtotal='10000', total_amount='10000')
    old = timezone.now() - timedelta(days=2)
    Order.objects.filter(pk=order.pk).update(
        created_at=old, updated_at=old, synced_at=old,
    )
    order.refresh_from_db()
    return order


@pytest.mark.parametrize(('field', 'value'), [
    ('description', 'Table moved'), ('total_amount', '12000'),
    ('status', Order.Status.READY),
])
def test_partial_order_changes_publish_a_new_report_timestamp(cached_order, field, value):
    order = cached_order
    previous = order.updated_at
    created_at = order.created_at
    setattr(order, field, value)
    order.save(update_fields=(field,))
    order.refresh_from_db()
    assert order.updated_at > previous
    assert order.created_at == created_at
    assert order.to_sync_dict()['updated_at'] == order.updated_at.isoformat()


def test_sync_acknowledgement_does_not_look_like_an_order_change(cached_order):
    order = cached_order
    previous = order.updated_at
    order.synced_at = timezone.now()
    order.save(update_fields=['synced_at'])
    order.refresh_from_db()
    assert order.updated_at == previous


def test_incoming_sync_keeps_the_source_timestamp(cached_order):
    order = cached_order
    source_time = order.updated_at + timedelta(hours=1)
    created_at = order.created_at
    payload = order.to_sync_dict()
    payload.update(sync_version=order.sync_version + 1,
                   status=Order.Status.READY, updated_at=source_time.isoformat())
    received, action = Order.from_sync_dict(payload, branch_id=order.branch_id)
    assert action == 'updated'
    received.refresh_from_db()
    assert received.status == Order.Status.READY
    assert received.updated_at == source_time
    assert received.created_at == created_at
