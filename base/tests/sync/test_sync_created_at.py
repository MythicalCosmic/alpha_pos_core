"""Sync must PRESERVE the origin created_at, not re-stamp it to the receiver's
clock on INSERT. Regression for: an offline till syncing a backlog dumped all of
yesterday's orders into today (created_at = receive time) and corrupted analytics."""
import uuid as uuidlib
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def _user():
    from base.models import User
    return User.objects.create(email=f'sy{uuidlib.uuid4().hex[:6]}@x.local', first_name='a',
                               last_name='b', role='CASHIER', status='ACTIVE', password='!')


def _order_payload(user, created):
    return {
        'uuid': str(uuidlib.uuid4()),
        'sync_version': 1,
        'branch_id': 'branch1',
        'created_at': created.isoformat(),
        'updated_at': created.isoformat(),
        'user_uuid': str(user.uuid),
        'cashier_uuid': str(user.uuid),
        'status': 'COMPLETED',
        'is_paid': True,
        'display_id': 5,
        'subtotal': '100',
        'total_amount': '100',
        'payment_method': 'CASH',
        'order_type': 'HALL',
    }


def test_from_sync_dict_create_preserves_created_at():
    from base.models import Order
    u = _user()
    yesterday = timezone.now() - timedelta(days=1)
    inst, action = Order.from_sync_dict(_order_payload(u, yesterday), branch_id='branch1')
    assert action == 'created' and inst is not None
    # created_at is the ORIGIN time, not re-stamped to ~now
    assert abs((inst.created_at - yesterday).total_seconds()) < 2
    assert (timezone.now() - inst.created_at).total_seconds() > 3600


def test_cloud_receiver_push_preserves_created_at():
    from base.models import Order
    from base.services.sync.receiver import CloudReceiver
    u = _user()
    yesterday = timezone.now() - timedelta(days=1)
    p = _order_payload(u, yesterday)
    CloudReceiver.receive_batch('order', 'branch1', [p])
    o = Order.objects.get(uuid=p['uuid'])
    assert abs((o.created_at - yesterday).total_seconds()) < 2      # the real corruption path


def test_updated_at_still_preserved_regression():
    from base.models import Order
    u = _user()
    yesterday = timezone.now() - timedelta(days=1)
    inst, _ = Order.from_sync_dict(_order_payload(u, yesterday), branch_id='branch1')
    assert abs((inst.updated_at - yesterday).total_seconds()) < 2
