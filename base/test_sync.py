"""Regression tests for the sync partial-failure data-loss bug.

Pre-fix: SyncService.push() removed the WHOLE batch from the durable queue on
any HTTP-200, even when the receiver rejected individual records. And
CloudReceiver.receive_batch reported the per-record errors but not which UUIDs
failed, so the pusher had no way to keep them. Net effect: every record in a
partial-failure batch was silently lost (never retried).
"""
import uuid as uuidlib

import pytest

pytestmark = pytest.mark.django_db


class TestReceiveBatchReportsFailedUuids:
    def test_failed_uuids_collected_for_rejected_records(self, monkeypatch):
        from base.services.sync.receiver import CloudReceiver

        good = str(uuidlib.uuid4())
        bad = str(uuidlib.uuid4())

        def fake_create_or_update(cls, model_class, data, branch_id):
            if data['uuid'] == bad:
                raise ValueError('boom')
            return None, 'created'

        monkeypatch.setattr(
            CloudReceiver, '_create_or_update',
            classmethod(fake_create_or_update),
        )

        result = CloudReceiver.receive_batch(
            'product', 'branch-1',
            [{'uuid': good, 'name': 'A'}, {'uuid': bad, 'name': 'B'}],
        )

        assert result['created'] == 1
        assert result['failed_uuids'] == [bad]
        assert len(result['errors']) == 1


class TestPushKeepsRejectedRecordsQueued:
    def test_partial_failure_does_not_purge_rejected_record(
        self, settings, monkeypatch,
    ):
        from base.services.sync import service as sync_service
        from base.services.sync.queue import SyncQueue
        from base.models import SyncQueueRecord

        settings.SYNC_ENABLED = True
        settings.DEPLOYMENT_MODE = 'local'
        settings.CLOUD_SYNC_URL = 'http://cloud.test'
        settings.CLOUD_SYNC_TOKEN = 'tok'

        good = str(uuidlib.uuid4())
        bad = str(uuidlib.uuid4())
        SyncQueue.add('product', good, {'uuid': good, 'name': 'A'})
        SyncQueue.add('product', bad, {'uuid': bad, 'name': 'B'})

        monkeypatch.setattr(sync_service, 'check_health', lambda: True)
        monkeypatch.setattr(sync_service.SyncService, '_notify_success', staticmethod(lambda *a, **k: None))
        monkeypatch.setattr(sync_service.SyncService, '_notify_error', staticmethod(lambda *a, **k: None))

        def fake_send_batch(model_name, records, retry=True):
            # Receiver applied the good record, rejected the bad one.
            return {
                'success': True, 'created': 1, 'updated': 0, 'skipped': 0,
                'errors': [f'{bad}: boom'], 'failed_uuids': [bad],
            }

        monkeypatch.setattr(sync_service, 'send_batch', fake_send_batch)

        result = sync_service.SyncService.push()

        remaining = {str(r.record_uuid) for r in SyncQueueRecord.objects.all()}
        assert good not in remaining, 'confirmed record should be removed from queue'
        assert bad in remaining, 'rejected record MUST stay queued (no data loss)'

        # The rejected record is marked for retry, not silently dropped.
        bad_row = SyncQueueRecord.objects.get(record_uuid=uuidlib.UUID(bad))
        assert bad_row.attempts >= 1
        assert result['failed'] == 1
        assert result['success'] is False


class TestPullCursorNotClobbered:
    """Regression: set_last_pull() used to stamp `last_pull` (the durable pull
    CURSOR) with the terminal's local now(), overwriting the cloud-clock
    frontier that the paging loop persists. With any terminal/cloud clock skew
    this silently skipped cloud-created records (a server-created user never
    arrived). set_last_pull now writes last_pull_at and leaves the cursor."""

    def test_set_last_pull_does_not_touch_cursor(self):
        from base.services.sync.status import SyncStatus

        SyncStatus.clear()
        # The paging loop persists a cloud-frontier cursor here.
        SyncStatus.update(last_pull='2026-06-07T10:00:00+00:00')
        # The end-of-pull status write must NOT overwrite that cursor.
        SyncStatus.set_last_pull(created=3, updated=1)

        data = SyncStatus.get()
        assert data['last_pull'] == '2026-06-07T10:00:00+00:00'
        assert data['last_pull_created'] == 3
        assert 'last_pull_at' in data
