"""Immutable service identity must survive both sync transports."""
import pytest
from django.utils import timezone

from base.models import Order, Shift, User
from base.services.sync.config import SYNC_ORDER
from base.services.sync.receiver import CloudReceiver

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('transport', ['push', 'pull'])
@pytest.mark.parametrize('replace', [False, True])
def test_sync_cannot_clear_or_reassign_waiter_identity(settings, transport, replace):
    settings.DEPLOYMENT_MODE = 'cloud' if transport == 'push' else 'local'
    first = User.objects.create(email='first-waiter@test.local', role='WAITER', branch_id='branch1')
    second = User.objects.create(email='second-waiter@test.local', role='WAITER', branch_id='branch1')
    shift1 = Shift.objects.create(user=first, start_time=timezone.now(), branch_id='branch1')
    shift2 = Shift.objects.create(user=second, start_time=timezone.now(), branch_id='branch1')
    order = Order.objects.create(user=first, waiter=first, waiter_shift=shift1, branch_id='branch1',
                                  waiter_policy_snapshot={'payment_mode': 'CASHIER_HANDOFF', 'version': 1})
    data = order.to_sync_dict()
    data.update(sync_version=order.sync_version+5, waiter_uuid=str(second.uuid) if replace else None,
                waiter_shift_uuid=str(shift2.uuid) if replace else None, waiter_policy_snapshot={})
    if transport == 'push':
        _, action = CloudReceiver._create_or_update(Order, data, branch_id='branch1')
    else:
        _, action = Order.from_sync_dict(data, branch_id='branch1')
    assert action == 'updated'
    order.refresh_from_db()
    assert order.waiter_id == first.pk and order.waiter_shift_id == shift1.pk
    assert order.waiter_policy_snapshot['payment_mode'] == 'CASHIER_HANDOFF'


def test_waiter_shift_is_synchronized_before_order():
    assert SYNC_ORDER.index('shift') < SYNC_ORDER.index('order')
