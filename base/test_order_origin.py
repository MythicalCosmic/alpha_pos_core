import uuid

import pytest

from base.models import Order, User


pytestmark = pytest.mark.django_db


def _user(email='origin@example.com'):
    return User.objects.create(
        first_name='Order',
        last_name='Origin',
        email=email,
        password='!',
        role=User.RoleChoices.CASHIER,
    )


def test_legacy_and_pos_orders_default_to_pos_origin():
    order = Order.objects.create(
        user=_user(),
        subtotal='10000.00',
        total_amount='10000.00',
    )

    assert order.order_origin == Order.Origin.POS


def test_order_origin_is_serialized_and_restored_by_sync(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user()
    source = Order.objects.create(
        user=user,
        order_origin=Order.Origin.TELEGRAM,
        branch_id='branch-a',
        subtotal='25000.00',
        total_amount='25000.00',
    )
    payload = source.to_sync_dict()

    assert payload['order_origin'] == Order.Origin.TELEGRAM

    source.delete()
    payload['uuid'] = str(uuid.uuid4())
    payload['user_uuid'] = str(user.uuid)
    instance, action = Order.from_sync_dict(payload, branch_id='branch-a')

    assert action == 'created'
    assert instance.order_origin == Order.Origin.TELEGRAM


def test_old_sync_payload_without_origin_remains_backward_compatible(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user()
    instance, action = Order.from_sync_dict({
        'uuid': str(uuid.uuid4()),
        'sync_version': 1,
        'branch_id': 'branch-a',
        'user_uuid': str(user.uuid),
        'subtotal': '12000.00',
        'total_amount': '12000.00',
    }, branch_id='branch-a')

    assert action == 'created'
    assert instance.order_origin == Order.Origin.POS
