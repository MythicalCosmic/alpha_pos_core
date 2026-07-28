"""Adversarial regressions for local generation and identity durability."""

import uuid

import pytest


pytestmark = pytest.mark.django_db


def _advance_order_out_of_band(order, *, sync_version):
    """Simulate a concurrent committed writer after ``order`` was loaded."""
    from base.models import Order

    canonical_uuid = uuid.uuid4()
    Order._base_manager.filter(pk=order.pk).update(
        uuid=canonical_uuid,
        status=Order.Status.COMPLETED,
        sync_version=sync_version,
    )
    return canonical_uuid


def test_stale_order_soft_delete_uses_persisted_uuid_and_next_version(
    settings,
    order_factory,
):
    from base.models import Order, SyncQueueRecord

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = True

    order = order_factory(status=Order.Status.READY)
    stale_uuid = order.uuid
    canonical_uuid = _advance_order_out_of_band(order, sync_version=17)
    SyncQueueRecord.objects.all().delete()

    order.delete()

    persisted = Order._base_manager.get(pk=order.pk)
    assert persisted.uuid == canonical_uuid
    assert persisted.status == Order.Status.COMPLETED
    assert persisted.is_deleted is True
    assert persisted.sync_version == 18
    queued = SyncQueueRecord.objects.get(
        model_name='order',
        record_uuid=canonical_uuid,
    )
    assert queued.payload['uuid'] == str(canonical_uuid)
    assert queued.payload['sync_version'] == 18
    assert queued.payload['is_deleted'] is True
    assert not SyncQueueRecord.objects.filter(
        model_name='order',
        record_uuid=stale_uuid,
    ).exists()


def test_stale_cloud_save_uses_persisted_uuid_and_next_version(
    settings,
    django_capture_on_commit_callbacks,
):
    from base.models import Category

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'

    stale = Category.objects.create(
        name='Original',
        slug='cloud-stale-save',
        sync_version=2,
    )
    canonical_uuid = uuid.uuid4()
    Category._base_manager.filter(pk=stale.pk).update(
        uuid=canonical_uuid,
        name='Concurrent cloud edit',
        sync_version=31,
    )

    stale.sort_order = 7
    with django_capture_on_commit_callbacks(execute=True):
        stale.save(update_fields=['sort_order'])

    persisted = Category._base_manager.get(pk=stale.pk)
    assert persisted.uuid == canonical_uuid
    assert persisted.name == 'Concurrent cloud edit'
    assert persisted.sort_order == 7
    assert persisted.sync_version == 32
    assert persisted.synced_at is not None


def test_stale_order_hard_delete_uses_persisted_uuid_and_next_version(
    settings,
    order_factory,
):
    from base.models import Order, SyncQueueRecord

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = True

    order = order_factory(status=Order.Status.READY)
    order_pk = order.pk
    stale_uuid = order.uuid
    canonical_uuid = _advance_order_out_of_band(order, sync_version=23)
    SyncQueueRecord.objects.all().delete()

    order.hard_delete()

    assert not Order._base_manager.filter(pk=order_pk).exists()
    queued = SyncQueueRecord.objects.get(
        model_name='order',
        record_uuid=canonical_uuid,
    )
    assert queued.payload['uuid'] == str(canonical_uuid)
    assert queued.payload['sync_version'] == 24
    assert queued.payload['is_deleted'] is True
    assert not SyncQueueRecord.objects.filter(
        model_name='order',
        record_uuid=stale_uuid,
    ).exists()


def test_disabled_transport_still_preserves_hard_delete_tombstone(
    settings,
    order_factory,
):
    from base.models import SyncQueueRecord

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = False

    order = order_factory()
    item = order.items.get()
    item_uuid = item.uuid
    next_version = item.sync_version + 1
    SyncQueueRecord.objects.all().delete()

    item.hard_delete()

    assert not type(item)._base_manager.filter(uuid=item_uuid).exists()
    queued = SyncQueueRecord.objects.get(
        model_name='orderitem',
        record_uuid=item_uuid,
    )
    assert queued.payload['is_deleted'] is True
    assert queued.payload['sync_version'] == next_version


def test_hard_delete_does_not_queue_cloud_or_local_only_rows(settings):
    from base.models import Category, SyncQueueRecord, TreasuryAccount

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    category = Category.objects.create(name='Cloud-only delete')
    SyncQueueRecord.objects.all().delete()

    category.hard_delete()

    assert not SyncQueueRecord.objects.exists()

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = False
    account = TreasuryAccount.objects.create(
        kind=TreasuryAccount.Kind.SAFE,
        branch_id='branch-a',
    )

    account.hard_delete()

    assert not SyncQueueRecord.objects.exists()


def _category_payload(record_uuid, *, slug, name='Cloud category'):
    return {
        'uuid': str(record_uuid),
        'sync_version': 9,
        'is_deleted': False,
        'branch_id': 'cloud',
        'name': name,
        'slug': slug,
        'sort_order': 0,
        'colors': [],
        'status': 'ACTIVE',
        'description': '',
    }


def test_natural_key_rekey_prefers_live_row_over_older_deleted_history(
    settings,
):
    from base.models import Category
    from base.services.sync.service import SyncService

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = False

    deleted = Category.objects.create(
        name='Deleted history',
        slug='same-slug',
        is_deleted=True,
        branch_id='cloud',
        sync_version=3,
    )
    deleted_uuid = deleted.uuid
    live = Category.objects.create(
        name='Current live',
        slug='same-slug',
        branch_id='cloud',
        sync_version=4,
    )
    incoming_uuid = uuid.uuid4()

    result = SyncService._apply_records(
        Category,
        [_category_payload(incoming_uuid, slug='same-slug')],
        source_branch='cloud',
        authoritative_cloud=True,
    )

    deleted.refresh_from_db()
    live.refresh_from_db()
    assert result['updated'] == 1
    assert result['errors'] == []
    assert deleted.uuid == deleted_uuid
    assert deleted.is_deleted is True
    assert deleted.name == 'Deleted history'
    assert live.uuid == incoming_uuid
    assert live.is_deleted is False
    assert live.name == 'Cloud category'


def test_only_deleted_natural_key_history_allows_fresh_identity(settings):
    from base.models import Category
    from base.services.sync.service import SyncService

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = False

    deleted = Category.objects.create(
        name='Deleted history',
        slug='reused-slug',
        is_deleted=True,
        branch_id='cloud',
        sync_version=20,
    )
    deleted_uuid = deleted.uuid
    incoming_uuid = uuid.uuid4()

    result = SyncService._apply_records(
        Category,
        [_category_payload(incoming_uuid, slug='reused-slug')],
        source_branch='cloud',
        authoritative_cloud=True,
    )

    deleted.refresh_from_db()
    fresh = Category._base_manager.get(uuid=incoming_uuid)
    assert result['created'] == 1
    assert result['errors'] == []
    assert deleted.uuid == deleted_uuid
    assert deleted.is_deleted is True
    assert deleted.name == 'Deleted history'
    assert fresh.pk != deleted.pk
    assert fresh.is_deleted is False
    assert fresh.slug == deleted.slug
    assert fresh.name == 'Cloud category'
