import uuid

import pytest

from base.models import SyncQueueRecord, User
from base.services.sync.queue import SyncQueue
from base.services.sync.service import SyncService


pytestmark = pytest.mark.django_db


def test_untrusted_nested_context_cannot_inherit_pull_authority():
    from base.services.sync.context import (
        authoritative_cloud_pull,
        is_authoritative_cloud_pull,
    )

    assert is_authoritative_cloud_pull() is False
    with authoritative_cloud_pull(True):
        assert is_authoritative_cloud_pull() is True
        with authoritative_cloud_pull(False):
            assert is_authoritative_cloud_pull() is False
        assert is_authoritative_cloud_pull() is True
    assert is_authoritative_cloud_pull() is False


def _user(*, email, sync_version, record_uuid=None):
    values = {
        'first_name': 'Local',
        'last_name': 'Identity',
        'email': email,
        'password': 'old-hash',
        'role': User.RoleChoices.CASHIER,
        'status': User.UserStatus.ACTIVE,
        'branch_id': 'cloud',
        'sync_version': sync_version,
    }
    if record_uuid is not None:
        values['uuid'] = record_uuid
    return User.objects.create(**values)


def _cloud_record(record_uuid, email, *, sync_version=3):
    return {
        'uuid': str(record_uuid),
        'sync_version': sync_version,
        'is_deleted': False,
        'first_name': 'Cloud',
        'last_name': 'Current',
        'email': email,
        'password': 'cloud-hash',
        'role': User.RoleChoices.CASHIER,
        'status': User.UserStatus.ACTIVE,
        'permissions': [],
        'branch_id': 'cloud',
    }


def test_global_pull_cleans_dead_letter_under_pre_rekey_uuid(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'main'

    local = _user(
        email='natural-rekey@test.local',
        sync_version=165,
    )
    old_uuid = local.uuid
    SyncQueue.add('user', old_uuid, local.to_sync_dict())
    SyncQueueRecord.objects.filter(
        model_name='user',
        record_uuid=old_uuid,
    ).update(
        attempts=25,
        last_error='[REJECTED] GLOBAL_MODEL_WRITE_REFUSED',
    )
    cloud_uuid = uuid.uuid4()

    result = SyncService._apply_records(
        User,
        [_cloud_record(cloud_uuid, local.email)],
        source_branch='cloud',
        authoritative_cloud=True,
    )

    local.refresh_from_db()
    assert result['updated'] == 1
    assert local.uuid == cloud_uuid
    assert local.first_name == 'Cloud'
    assert not SyncQueueRecord.objects.filter(
        model_name='user',
        record_uuid=old_uuid,
    ).exists()
    assert SyncQueue.dead_letter_count() == 0


def test_global_pull_cleanup_preserves_concurrent_replacement_generation(
    settings,
    monkeypatch,
):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'main'

    local = _user(
        email='generation-race@test.local',
        sync_version=2,
    )
    SyncQueue.add('user', local.uuid, local.to_sync_dict())
    old_generation = SyncQueueRecord.objects.get(
        model_name='user',
        record_uuid=local.uuid,
    ).generation
    original_from_sync = User.from_sync_dict.__func__

    def apply_then_replace_queue(cls, data, branch_id=None):
        instance, action = original_from_sync(
            cls,
            data,
            branch_id=branch_id,
        )
        # Simulate a local save landing after the pull applied the cloud row but
        # before stale-queue cleanup. Its new generation must remain observable.
        User._base_manager.filter(pk=instance.pk).update(
            first_name='Concurrent local edit',
            sync_version=4,
            synced_at=None,
        )
        replacement = instance.to_sync_dict()
        replacement['first_name'] = 'Concurrent local edit'
        replacement['sync_version'] = 4
        SyncQueue.add('user', instance.uuid, replacement)
        return instance, action

    monkeypatch.setattr(
        User,
        'from_sync_dict',
        classmethod(apply_then_replace_queue),
    )

    result = SyncService._apply_records(
        User,
        [_cloud_record(local.uuid, local.email)],
        source_branch='cloud',
        authoritative_cloud=True,
    )

    queued = SyncQueueRecord.objects.get(
        model_name='user',
        record_uuid=local.uuid,
    )
    assert result['updated'] == 1
    assert queued.generation != old_generation
    assert queued.payload['sync_version'] == 4
    assert queued.payload['first_name'] == 'Concurrent local edit'


def test_authoritative_context_resets_after_apply_failure(settings, monkeypatch):
    from base.services.sync.context import is_authoritative_cloud_pull

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'main'
    cloud_uuid = uuid.uuid4()

    def fail_inside_pull(cls, data, branch_id=None):
        assert is_authoritative_cloud_pull() is True
        raise RuntimeError('simulated apply failure')

    monkeypatch.setattr(
        User,
        'from_sync_dict',
        classmethod(fail_inside_pull),
    )

    result = SyncService._apply_records(
        User,
        [_cloud_record(cloud_uuid, 'context-reset@test.local')],
        source_branch='cloud',
        authoritative_cloud=True,
    )

    assert result['errors']
    assert is_authoritative_cloud_pull() is False


def test_global_cleanup_snapshot_and_ack_share_record_transaction(
    settings,
    monkeypatch,
):
    from django.db import transaction

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'main'
    local = _user(
        email='locked-cleanup@test.local',
        sync_version=20,
    )
    SyncQueue.add('user', local.uuid, local.to_sync_dict())

    original_snapshots = SyncQueue.get_snapshots.__func__
    original_acknowledge = SyncQueue.acknowledge.__func__
    observations = []

    def checked_snapshots(cls, model_name, uuids, *, lock=False):
        observations.append(
            ('snapshot', lock, transaction.get_connection().in_atomic_block)
        )
        return original_snapshots(
            cls,
            model_name,
            uuids,
            lock=lock,
        )

    def checked_acknowledge(cls, records, model_name):
        observations.append(
            ('ack', True, transaction.get_connection().in_atomic_block)
        )
        return original_acknowledge(cls, records, model_name)

    monkeypatch.setattr(
        SyncQueue,
        'get_snapshots',
        classmethod(checked_snapshots),
    )
    monkeypatch.setattr(
        SyncQueue,
        'acknowledge',
        classmethod(checked_acknowledge),
    )

    result = SyncService._apply_records(
        User,
        [_cloud_record(local.uuid, local.email)],
        source_branch='cloud',
        authoritative_cloud=True,
    )

    assert result['updated'] == 1
    assert ('snapshot', True, True) in observations
    assert ('ack', True, True) in observations
