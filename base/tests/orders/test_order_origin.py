import uuid

import pytest

from base.models import Order, User
from base.services.sync.receiver import CloudReceiver


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


def test_cloud_accepts_qr_origin_on_branch_create(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    user = _user('qr-origin@example.com')
    payload = {
        'uuid': str(uuid.uuid4()),
        'sync_version': 1,
        'branch_id': 'branch-a',
        'user_uuid': str(user.uuid),
        'order_origin': Order.Origin.QR,
        'subtotal': '18000.00',
        'total_amount': '18000.00',
    }

    instance, action = CloudReceiver._create_or_update(
        Order, payload, 'branch-a',
    )

    assert action == 'created'
    assert instance.order_origin == Order.Origin.QR


def test_cloud_rejects_branch_claiming_telegram_origin(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    user = _user('forged-telegram-origin@example.com')
    payload = {
        'uuid': str(uuid.uuid4()),
        'sync_version': 1,
        'branch_id': 'branch-a',
        'user_uuid': str(user.uuid),
        'order_origin': Order.Origin.TELEGRAM,
        'subtotal': '18000.00',
        'total_amount': '18000.00',
    }

    instance, action = CloudReceiver._create_or_update(
        Order, payload, 'branch-a',
    )

    assert action == 'skipped'
    assert instance is None
    assert not Order.objects.filter(uuid=payload['uuid']).exists()


@pytest.mark.parametrize('incoming_origin', [
    Order.Origin.POS,
    # A compromised/newer branch must not change one remote producer into
    # another either; creation is the only branch-owned origin decision.
    Order.Origin.QR,
])
def test_branch_update_cannot_rewrite_cloud_telegram_origin(
    settings, incoming_origin,
):
    settings.DEPLOYMENT_MODE = 'cloud'
    user = _user(f'preserve-{incoming_origin.lower()}@example.com')
    order = Order.objects.create(
        user=user,
        branch_id='branch-a',
        order_origin=Order.Origin.TELEGRAM,
        status=Order.Status.OPEN,
        subtotal='22000.00',
        total_amount='22000.00',
        sync_version=3,
    )
    payload = order.to_sync_dict()
    payload.update({
        'sync_version': 4,
        'order_origin': incoming_origin,
        'status': Order.Status.PREPARING,
    })

    instance, action = CloudReceiver._create_or_update(
        Order, payload, 'branch-a',
    )

    assert action == 'updated'
    assert instance.status == Order.Status.PREPARING
    assert instance.sync_version == 4
    assert instance.order_origin == Order.Origin.TELEGRAM


def test_direct_cloud_ingest_applies_same_create_only_origin_policy(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    user = _user('direct-origin-policy@example.com')
    order = Order.objects.create(
        user=user,
        branch_id='branch-a',
        order_origin=Order.Origin.TELEGRAM,
        subtotal='24000.00',
        total_amount='24000.00',
        sync_version=2,
    )
    payload = order.to_sync_dict()
    payload.update({
        'sync_version': 3,
        'order_origin': Order.Origin.POS,
        'status': Order.Status.READY,
    })

    instance, action = Order.from_sync_dict(payload, branch_id='branch-a')

    assert action == 'updated'
    assert instance.status == Order.Status.READY
    assert instance.order_origin == Order.Origin.TELEGRAM


def test_upgraded_terminal_converges_equal_version_pos_default_to_telegram(
    settings,
):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user('upgrade-origin@example.com')
    order = Order.objects.create(
        user=user,
        branch_id='branch-a',
        order_origin=Order.Origin.POS,
        subtotal='26000.00',
        total_amount='26000.00',
        sync_version=7,
    )
    payload = order.to_sync_dict()
    payload['order_origin'] = Order.Origin.TELEGRAM

    instance, action = Order.from_sync_dict(payload, branch_id='branch-a')

    assert action == 'updated'
    assert instance.sync_version == 7
    assert instance.order_origin == Order.Origin.TELEGRAM


def test_equal_version_origin_repair_does_not_overwrite_other_local_fields(
    settings,
):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user('origin-only-repair@example.com')
    order = Order.objects.create(
        user=user,
        branch_id='branch-a',
        order_origin=Order.Origin.POS,
        status=Order.Status.READY,
        description='newer local note',
        subtotal='30000.00',
        total_amount='30000.00',
        sync_version=9,
    )
    payload = order.to_sync_dict()
    payload.update({
        'order_origin': Order.Origin.TELEGRAM,
        'status': Order.Status.OPEN,
        'description': 'stale cloud note',
    })

    instance, action = Order.from_sync_dict(payload, branch_id='branch-a')

    assert action == 'updated'
    assert instance.order_origin == Order.Origin.TELEGRAM
    assert instance.status == Order.Status.READY
    assert instance.description == 'newer local note'


def test_equal_version_origin_repair_never_reclassifies_known_remote_origin(
    settings,
):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user('known-origin@example.com')
    order = Order.objects.create(
        user=user,
        branch_id='branch-a',
        order_origin=Order.Origin.QR,
        subtotal='31000.00',
        total_amount='31000.00',
        sync_version=10,
    )
    payload = order.to_sync_dict()
    payload['order_origin'] = Order.Origin.TELEGRAM

    instance, action = Order.from_sync_dict(payload, branch_id='branch-a')

    assert action == 'skipped'
    assert instance.order_origin == Order.Origin.QR


def test_equal_version_origin_repair_cannot_cross_branch(settings):
    settings.DEPLOYMENT_MODE = 'local'
    user = _user('cross-branch-origin@example.com')
    order = Order.objects.create(
        user=user,
        branch_id='branch-a',
        order_origin=Order.Origin.POS,
        subtotal='32000.00',
        total_amount='32000.00',
        sync_version=11,
    )
    payload = order.to_sync_dict()
    payload.update({
        'branch_id': 'branch-b',
        'order_origin': Order.Origin.TELEGRAM,
    })

    instance, action = Order.from_sync_dict(payload, branch_id='branch-b')

    assert action == 'skipped'
    assert instance.order_origin == Order.Origin.POS


def test_terminal_does_not_take_remote_origin_from_older_version(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user('stale-origin@example.com')
    order = Order.objects.create(
        user=user,
        branch_id='branch-a',
        order_origin=Order.Origin.POS,
        subtotal='28000.00',
        total_amount='28000.00',
        sync_version=8,
    )
    payload = order.to_sync_dict()
    payload.update({
        'sync_version': 7,
        'order_origin': Order.Origin.TELEGRAM,
    })

    instance, action = Order.from_sync_dict(payload, branch_id='branch-a')

    assert action == 'skipped'
    assert instance.order_origin == Order.Origin.POS
