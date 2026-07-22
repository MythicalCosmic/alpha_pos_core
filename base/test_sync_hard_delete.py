import pytest


@pytest.mark.django_db
def test_legacy_hard_delete_queues_tombstone_when_on_save_is_disabled(
    settings, order_factory, django_capture_on_commit_callbacks,
):
    from base.models import SyncQueueRecord

    settings.DEPLOYMENT_MODE = 'local'
    settings.SYNC_ENABLED = True
    settings.SYNC_ON_SAVE = False
    order = order_factory()
    item = order.items.get()
    item_uuid = item.uuid
    SyncQueueRecord.objects.all().delete()

    with django_capture_on_commit_callbacks(execute=True):
        item.delete(hard_delete=True)

    assert not type(item).objects.filter(uuid=item_uuid).exists()
    queued = SyncQueueRecord.objects.get(
        model_name='orderitem', record_uuid=item_uuid,
    )
    assert queued.payload['uuid'] == str(item_uuid)
    assert queued.payload['is_deleted'] is True


@pytest.mark.django_db
def test_rolled_back_hard_delete_does_not_leave_orphan_tombstone(
    settings, order_factory, django_capture_on_commit_callbacks,
):
    from django.db import transaction
    from base.models import SyncQueueRecord

    settings.DEPLOYMENT_MODE = 'local'
    settings.SYNC_ENABLED = True
    settings.SYNC_ON_SAVE = False
    order = order_factory()
    item = order.items.get()
    item_uuid = item.uuid
    SyncQueueRecord.objects.all().delete()

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                item.hard_delete()
                raise RuntimeError('force rollback')

    assert type(item).objects.filter(uuid=item_uuid).exists()
    assert not SyncQueueRecord.objects.filter(
        model_name='orderitem', record_uuid=item_uuid,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_failed_autocommit_hard_delete_never_queues_orphan_tombstone(
    settings, order_factory, monkeypatch,
):
    from django.db import models
    from base.models import SyncQueueRecord

    settings.DEPLOYMENT_MODE = 'local'
    settings.SYNC_ENABLED = True
    settings.SYNC_ON_SAVE = False
    order = order_factory()
    item = order.items.get()
    item_uuid = item.uuid
    SyncQueueRecord.objects.all().delete()

    def fail_delete(*args, **kwargs):
        raise RuntimeError('simulated database delete failure')

    monkeypatch.setattr(models.Model, 'delete', fail_delete)

    with pytest.raises(RuntimeError, match='simulated database delete failure'):
        item.hard_delete()

    assert type(item).objects.filter(uuid=item_uuid).exists()
    assert not SyncQueueRecord.objects.filter(
        model_name='orderitem', record_uuid=item_uuid,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_fails_closed_when_tombstone_cannot_be_prepared(
    settings, order_factory, monkeypatch,
):
    from base.models import SyncQueueRecord

    settings.DEPLOYMENT_MODE = 'local'
    settings.SYNC_ENABLED = True
    settings.SYNC_ON_SAVE = False
    order = order_factory()
    item = order.items.get()
    item_uuid = item.uuid
    SyncQueueRecord.objects.all().delete()

    def fail_serialization():
        raise RuntimeError('cannot serialize tombstone')

    monkeypatch.setattr(item, 'to_sync_dict', fail_serialization)

    with pytest.raises(RuntimeError, match='cannot serialize tombstone'):
        item.hard_delete()

    assert type(item).objects.filter(uuid=item_uuid).exists()
    assert not SyncQueueRecord.objects.filter(
        model_name='orderitem', record_uuid=item_uuid,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_rolls_back_when_durable_tombstone_enqueue_fails(
    settings, order_factory, monkeypatch,
):
    from base.models import SyncQueueRecord
    from base.services.sync.service import SyncService

    settings.DEPLOYMENT_MODE = 'local'
    settings.SYNC_ENABLED = True
    settings.SYNC_ON_SAVE = False
    order = order_factory()
    item = order.items.get()
    item_uuid = item.uuid
    SyncQueueRecord.objects.all().delete()

    def fail_enqueue(cls, model_name, uuid_val, payload):
        raise RuntimeError('simulated durable queue failure')

    monkeypatch.setattr(SyncService, 'queue_tombstone', classmethod(fail_enqueue))

    with pytest.raises(RuntimeError, match='simulated durable queue failure'):
        item.hard_delete()

    assert type(item).objects.filter(uuid=item_uuid).exists()
    assert not SyncQueueRecord.objects.filter(
        model_name='orderitem', record_uuid=item_uuid,
    ).exists()


@pytest.mark.django_db
def test_queue_clear_preserves_source_less_hard_delete_tombstone(
    settings, order_factory,
):
    """Queue maintenance may drop snapshots, never the only delete marker."""
    from base.models import SyncQueueRecord
    from base.services.sync.queue import SyncQueue

    settings.DEPLOYMENT_MODE = 'local'
    settings.SYNC_ENABLED = True
    order = order_factory()
    item = order.items.get()
    item_uuid = item.uuid
    SyncQueueRecord.objects.all().delete()

    item.hard_delete()
    live_uuid = order.uuid
    SyncQueue.add('order', live_uuid, order.to_sync_dict())

    cleared = SyncQueue.clear()

    assert cleared == 1
    assert not SyncQueueRecord.objects.filter(
        model_name='order', record_uuid=live_uuid,
    ).exists()
    tombstone = SyncQueueRecord.objects.get(
        model_name='orderitem', record_uuid=item_uuid,
    )
    assert tombstone.payload['is_deleted'] is True
