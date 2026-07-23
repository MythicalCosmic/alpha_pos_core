"""Regression coverage for sync ownership and generic ingest invariants."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _user(email=None):
    from base.models import User

    return User.objects.create(
        first_name='Sync', last_name='Owner',
        email=email or f'sync-{uuid.uuid4().hex}@example.com',
        password='!', role='CASHIER', status='ACTIVE',
    )


def test_cloud_refuses_cross_branch_uuid_overwrite(settings):
    from base.models import Order
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    user = _user()
    victim = Order.objects.create(
        user=user, branch_id='branch-b', description='branch B evidence',
        subtotal='50000', total_amount='50000',
    )
    payload = victim.to_sync_dict()
    payload.update({
        'branch_id': 'branch-a',
        'sync_version': victim.sync_version + 10,
        'description': 'forged overwrite',
        'is_deleted': True,
    })

    instance, action = CloudReceiver._create_or_update(
        Order, payload, branch_id='branch-a',
    )

    assert instance.pk == victim.pk
    assert action == 'skipped'
    victim.refresh_from_db()
    assert victim.description == 'branch B evidence'
    assert victim.is_deleted is False
    assert victim.branch_id == 'branch-b'


def test_cloud_rejects_child_link_to_other_branch_parent(settings):
    from base.models import Category, Order, OrderItem, Product
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    user = _user()
    category = Category.objects.create(name='Shared')
    product = Product.objects.create(
        category=category, name='Burger', price='50000',
    )
    other_order = Order.objects.create(
        user=user, branch_id='branch-b', subtotal='50000', total_amount='50000',
    )
    payload = {
        'uuid': str(uuid.uuid4()),
        'sync_version': 1,
        'branch_id': 'branch-a',
        'order_uuid': str(other_order.uuid),
        'product_uuid': str(product.uuid),
        'quantity': 1,
        'price': '50000',
    }

    result = CloudReceiver.receive_batch(
        'orderitem', branch_id='branch-a', records=[payload],
    )
    assert result['skipped'] == 1
    assert result['acknowledged_uuids'] == []
    assert result['rejected_uuids'] == [payload['uuid']]
    assert result['failed_uuids'] == [payload['uuid']]
    assert result['record_results'][0]['reason_code'] == 'CROSS_BRANCH_PARENT'
    assert not OrderItem.objects.filter(uuid=payload['uuid']).exists()


def test_local_pull_rejects_child_link_to_peer_parent(settings):
    from base.models import Category, Order, OrderItem, Product

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user()
    category = Category.objects.create(name='Shared local')
    product = Product.objects.create(
        category=category, name='Burger', price='50000',
    )
    peer_order = Order.objects.create(
        user=user, branch_id='branch-b', subtotal='50000', total_amount='50000',
    )

    instance, action = OrderItem.from_sync_dict({
        'uuid': str(uuid.uuid4()),
        'sync_version': 1,
        'branch_id': 'branch-a',
        'order_uuid': str(peer_order.uuid),
        'product_uuid': str(product.uuid),
        'quantity': 1,
        'price': '50000',
    }, branch_id='branch-a')

    assert instance is None
    assert action == 'skipped'


def test_local_pull_skips_record_targeted_to_peer_branch(settings):
    from base.models import Order
    from base.services.sync.service import SyncService

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    record_uuid = str(uuid.uuid4())

    result = SyncService._apply_records(Order, [{
        'uuid': record_uuid,
        'branch_id': 'branch-b',
        'sync_version': 1,
    }])

    assert result['skipped'] == 1
    assert not result['deferred']
    assert not Order.objects.filter(uuid=record_uuid).exists()


def test_local_pull_refuses_existing_uuid_owned_by_peer_branch(settings):
    from base.models import Order

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user()
    victim = Order.objects.create(
        user=user, branch_id='branch-b', description='peer evidence',
        subtotal='50000', total_amount='50000', sync_version=1,
    )
    payload = victim.to_sync_dict()
    payload.update({
        'branch_id': 'branch-a',
        'sync_version': 99,
        'description': 'cross-branch overwrite',
    })

    instance, action = Order.from_sync_dict(
        payload, branch_id='branch-a',
    )

    assert instance.pk == victim.pk
    assert action == 'skipped'
    victim.refresh_from_db()
    assert victim.branch_id == 'branch-b'
    assert victim.description == 'peer evidence'
    assert victim.sync_version == 1


def test_missing_optional_order_fk_defers_instead_of_losing_attribution(settings):
    from base.models import Order

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    user = _user()
    record_uuid = str(uuid.uuid4())
    instance, action = Order.from_sync_dict({
        'uuid': record_uuid,
        'branch_id': 'branch-a',
        'sync_version': 1,
        'user_uuid': str(user.uuid),
        'cashier_uuid': str(uuid.uuid4()),
        'status': 'READY',
        'subtotal': '50000',
        'total_amount': '50000',
    }, branch_id='branch-a')

    assert instance is None
    assert action == 'deferred'
    assert not Order.objects.filter(uuid=record_uuid).exists()


def test_generic_contract_keeps_tombstone_terminal(settings):
    from base.models import Category, Product

    settings.DEPLOYMENT_MODE = 'local'
    category = Category.objects.create(name='Catalog')
    product = Product.objects.create(
        category=category, name='Deleted', price='1', is_deleted=True,
        sync_version=2,
    )
    payload = product.to_sync_dict()
    payload.update({
        'sync_version': 3,
        'is_deleted': False,
        'name': 'Resurrected',
    })

    instance, action = Product.from_sync_dict(
        payload, branch_id=payload['branch_id'],
    )

    assert action == 'skipped'
    instance.refresh_from_db()
    assert instance.is_deleted is True
    assert instance.name == 'Deleted'


def test_equal_version_cloud_catalog_change_wins_on_branch(settings):
    from base.models import Category, Product

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    category = Category.objects.create(name='Catalog')
    product = Product.objects.create(
        category=category, name='Old menu name', price='50000',
        branch_id='branch-a', sync_version=4,
    )
    payload = product.to_sync_dict()
    payload.update({
        'branch_id': 'cloud',
        'sync_version': 4,
        'name': 'Authoritative menu name',
        'price': '55000',
    })

    instance, action = Product.from_sync_dict(payload, branch_id='cloud')

    assert action == 'updated'
    instance.refresh_from_db()
    assert instance.name == 'Authoritative menu name'
    assert instance.price == 55000


@pytest.mark.parametrize('receiver_path', ['pull', 'push'])
def test_nonstandard_automatic_event_time_is_preserved(
    settings, receiver_path,
):
    from base.models import Inkassa
    from base.services.sync.receiver import CloudReceiver

    event_time = timezone.now() - timedelta(days=3)
    payload = {
        'uuid': str(uuid.uuid4()),
        'sync_version': 1,
        'branch_id': 'branch-a',
        'cashier_uuid': None,
        'amount': '50000',
        'inkass_type': 'CASH',
        'balance_before': '100000',
        'balance_after': '50000',
        'period_end': event_time.isoformat(),
        'created_at': event_time.isoformat(),
    }

    if receiver_path == 'pull':
        settings.DEPLOYMENT_MODE = 'local'
        settings.BRANCH_ID = 'branch-a'
        instance, action = Inkassa.from_sync_dict(
            payload, branch_id='branch-a',
        )
    else:
        settings.DEPLOYMENT_MODE = 'cloud'
        instance, action = CloudReceiver._create_or_update(
            Inkassa, payload, branch_id='branch-a',
        )

    assert action == 'created'
    instance.refresh_from_db()
    assert abs((instance.period_end - event_time).total_seconds()) < 1
    assert abs((instance.created_at - event_time).total_seconds()) < 1
