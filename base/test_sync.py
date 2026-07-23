"""Regression tests for the sync partial-failure data-loss bug.

Pre-fix: SyncService.push() removed the WHOLE batch from the durable queue on
any HTTP-200, even when the receiver rejected individual records. And
CloudReceiver.receive_batch reported the per-record errors but not which UUIDs
failed, so the pusher had no way to keep them. Net effect: every record in a
partial-failure batch was silently lost (never retried).
"""
import uuid as uuidlib

import pytest
from django.utils import timezone

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
                'acknowledged_uuids': [good],
                'retryable_uuids': [],
                'rejected_uuids': [bad],
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

    def test_partial_parent_failure_defers_dependent_models(
        self, settings, monkeypatch,
    ):
        from base.services.sync import service as sync_service
        from base.services.sync.queue import SyncQueue
        from base.models import SyncQueueRecord

        settings.SYNC_ENABLED = True
        settings.DEPLOYMENT_MODE = 'local'
        settings.CLOUD_SYNC_URL = 'http://cloud.test'
        settings.CLOUD_SYNC_TOKEN = 'tok'

        parent = str(uuidlib.uuid4())
        child = str(uuidlib.uuid4())
        SyncQueue.add('order', parent, {'uuid': parent, 'sync_version': 1})
        SyncQueue.add(
            'orderitem', child,
            {'uuid': child, 'order_uuid': parent, 'sync_version': 1},
        )

        monkeypatch.setattr(sync_service, 'check_health', lambda: True)
        monkeypatch.setattr(
            sync_service.SyncService, '_reconcile_unsynced',
            classmethod(lambda cls: None),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_notify_success', staticmethod(lambda *a, **k: None),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_notify_error', staticmethod(lambda *a, **k: None),
        )
        sent_models = []

        def fake_send_batch(model_name, records, retry=True):
            sent_models.append(model_name)
            assert model_name == 'order'
            return {
                'success': True, 'created': 0, 'updated': 0, 'skipped': 0,
                'errors': [f'{parent}: parent rejected'],
                'failed_uuids': [parent],
                'acknowledged_uuids': [],
                'retryable_uuids': [],
                'rejected_uuids': [parent],
            }

        monkeypatch.setattr(sync_service, 'send_batch', fake_send_batch)

        result = sync_service.SyncService.push()

        assert result['success'] is False
        assert sent_models == ['order']
        parent_row = SyncQueueRecord.objects.get(
            model_name='order', record_uuid=uuidlib.UUID(parent),
        )
        child_row = SyncQueueRecord.objects.get(
            model_name='orderitem', record_uuid=uuidlib.UUID(child),
        )
        # Protocol v2 distinguishes a permanent receiver rejection from a
        # retryable dependency. Permanently invalid generations become visible
        # dead letters immediately instead of burning one attempt per cycle.
        assert parent_row.attempts == settings.SYNC_MAX_QUEUE_ATTEMPTS
        assert parent_row.last_error.startswith('[REJECTED]')
        assert child_row.attempts == 0

    def test_systemic_failures_never_dead_letter_valid_money_records(
        self, settings, monkeypatch,
    ):
        """A repaired token/server must resume the exact unchanged payload.

        ``/health`` is intentionally public and can return 200 while /receive
        rejects an expired branch token.  Before this regression fix, 25 such
        cycles consumed the *record* poison budget and permanently hid valid
        orders/payments from every later push.
        """
        from base.models import SyncQueueRecord
        from base.services.sync import service as sync_service
        from base.services.sync.queue import SyncQueue

        settings.SYNC_ENABLED = True
        settings.DEPLOYMENT_MODE = 'local'
        settings.BRANCH_ID = 'main'
        settings.CLOUD_SYNC_URL = 'https://cloud.test'
        settings.CLOUD_SYNC_TOKEN = 'rotated-or-expired'
        settings.SYNC_MAX_QUEUE_ATTEMPTS = 2

        payment_uuid = str(uuidlib.uuid4())
        SyncQueue.add(
            'orderpayment', payment_uuid,
            {
                'uuid': payment_uuid,
                'sync_version': 1,
                'order_uuid': str(uuidlib.uuid4()),
                'method': 'CASH',
                'amount': '50000.00',
            },
        )

        monkeypatch.setattr(sync_service, 'check_health', lambda: True)
        monkeypatch.setattr(
            sync_service.SyncService, '_reconcile_unsynced',
            classmethod(lambda cls: None),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_notify_success',
            staticmethod(lambda *a, **k: None),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_notify_error',
            staticmethod(lambda *a, **k: None),
        )

        calls = 0

        def systemic_then_recovered(model_name, records, retry=True):
            nonlocal calls
            calls += 1
            if calls <= settings.SYNC_MAX_QUEUE_ATTEMPTS + 1:
                return {
                    'success': False,
                    'error': 'HTTP 401: Invalid branch token',
                }
            return {
                'success': True, 'created': 1, 'updated': 0, 'skipped': 0,
                'errors': [], 'failed_uuids': [],
                'acknowledged_uuids': [
                    str(record['uuid']) for record in records
                ],
                'retryable_uuids': [],
                'rejected_uuids': [],
            }

        monkeypatch.setattr(sync_service, 'send_batch', systemic_then_recovered)

        for _ in range(settings.SYNC_MAX_QUEUE_ATTEMPTS + 1):
            result = sync_service.SyncService.push()
            assert result['success'] is False
            row = SyncQueueRecord.objects.get(
                model_name='orderpayment', record_uuid=payment_uuid,
            )
            assert row.attempts == 0
            assert '401' in row.last_error
            assert SyncQueue.dead_letter_count() == 0
            assert 'orderpayment' in SyncQueue.get_grouped()

        recovered = sync_service.SyncService.push()
        assert recovered['success'] is True
        assert not SyncQueueRecord.objects.filter(
            model_name='orderpayment', record_uuid=payment_uuid,
        ).exists()


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


class TestPullTruthfulFailureStatus:
    def _configure(self, settings, monkeypatch):
        from base.services.sync import service as sync_service
        from base.services.sync.status import SyncStatus

        settings.SYNC_ENABLED = True
        settings.SYNC_PULL_ENABLED = True
        settings.DEPLOYMENT_MODE = 'local'
        settings.BRANCH_ID = 'main'
        settings.CLOUD_SYNC_URL = 'https://cloud.test'
        monkeypatch.setattr(sync_service, 'check_health', lambda: True)
        monkeypatch.setattr(sync_service, 'SYNC_ORDER', [])
        monkeypatch.setattr(sync_service, 'get_all_models', lambda: {})
        monkeypatch.setattr(
            sync_service.SyncService, '_acquire_lock',
            classmethod(lambda cls, name: 'owner'),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_renew_lock',
            classmethod(lambda cls, name, token: True),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_release_lock',
            classmethod(lambda cls, name, token: None),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_notify_error',
            classmethod(lambda cls, *args: None),
        )
        SyncStatus.clear()
        return sync_service, SyncStatus

    def test_fetch_error_is_persisted_in_status(self, settings, monkeypatch):
        sync_service, status = self._configure(settings, monkeypatch)
        monkeypatch.setattr(sync_service, 'fetch_changes', lambda **kwargs: {
            'success': False,
            'error': 'TLS certificate rejected',
        })

        result = sync_service.SyncService.pull_from_cloud()

        assert result['success'] is False
        assert result['message'] == 'TLS certificate rejected'
        assert status.get()['last_pull_error'] == 'TLS certificate rejected'

    def test_nonadvancing_feed_is_reported_as_failure(self, settings, monkeypatch):
        sync_service, status = self._configure(settings, monkeypatch)
        monkeypatch.setattr(sync_service, 'fetch_changes', lambda **kwargs: {
            'success': True,
            'data': {},
            'has_more': True,
            'next_since': None,
            'server_timestamp': '2026-07-14T00:00:00+00:00',
        })

        result = sync_service.SyncService.pull_from_cloud()

        assert result['success'] is False
        assert result['errors'] == ['Cloud change feed cursor did not advance']
        assert status.get()['last_pull_error'] == result['errors'][0]


class TestPullCursorWaitsForDeferredRecords:
    def _configure(self, settings, monkeypatch, apply_results):
        from base.services.sync import service as sync_service
        from base.services.sync.status import SyncStatus

        settings.SYNC_ENABLED = True
        settings.SYNC_PULL_ENABLED = True
        settings.DEPLOYMENT_MODE = 'local'
        settings.CLOUD_SYNC_URL = 'http://cloud.test'
        settings.CLOUD_SYNC_TOKEN = 'tok'
        monkeypatch.setattr(sync_service, 'check_health', lambda: True)
        monkeypatch.setattr(sync_service, 'SYNC_ORDER', ['child'])
        monkeypatch.setattr(sync_service, 'get_all_models', lambda: {'child': object})
        monkeypatch.setattr(
            sync_service.SyncService, '_acquire_lock',
            classmethod(lambda cls, name: 'owner'),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_renew_lock',
            classmethod(lambda cls, name, token: True),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_release_lock',
            classmethod(lambda cls, name, token: None),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_notify_pull_success',
            classmethod(lambda cls, *args: None),
        )
        monkeypatch.setattr(
            sync_service.SyncService, '_notify_error',
            classmethod(lambda cls, *args: None),
        )

        responses = iter(apply_results)
        monkeypatch.setattr(
            sync_service.SyncService, '_apply_records',
            classmethod(lambda cls, model, records: next(responses)),
        )
        monkeypatch.setattr(sync_service, 'fetch_changes', lambda **kwargs: {
            'success': True,
            'data': {'child': [{'uuid': str(uuidlib.uuid4())}]},
            'has_more': False,
            'next_since': None,
            'server_timestamp': '2026-07-13T12:00:00+00:00',
        })
        SyncStatus.clear()
        SyncStatus.set_cursor('2026-07-13T11:00:00+00:00')
        return sync_service, SyncStatus

    def test_unresolved_record_holds_back_cursor(self, settings, monkeypatch):
        deferred = {
            'created': 0, 'updated': 0, 'skipped': 0,
            'errors': [], 'deferred': [{'uuid': 'child'}],
        }
        sync_service, status = self._configure(
            settings, monkeypatch, [deferred, deferred],
        )

        result = sync_service.SyncService.pull_from_cloud()

        assert result['success'] is False
        assert result['errors']
        assert status.get_cursor() == '2026-07-13T11:00:00+00:00'

    def test_resolved_retry_publishes_terminal_cursor(self, settings, monkeypatch):
        deferred = {
            'created': 0, 'updated': 0, 'skipped': 0,
            'errors': [], 'deferred': [{'uuid': 'child'}],
        }
        resolved = {
            'created': 1, 'updated': 0, 'skipped': 0,
            'errors': [], 'deferred': [],
        }
        sync_service, status = self._configure(
            settings, monkeypatch, [deferred, resolved],
        )

        result = sync_service.SyncService.pull_from_cloud()

        assert result['success'] is True
        assert not result['errors']
        assert status.get_cursor() == '2026-07-13T12:00:00+00:00'


def _configure_local_push(settings, monkeypatch):
    from base.services.sync import service as sync_service

    settings.SYNC_ENABLED = True
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'main'
    settings.CLOUD_SYNC_URL = 'http://cloud.test'
    settings.CLOUD_SYNC_TOKEN = 'tok'
    monkeypatch.setattr(sync_service, 'check_health', lambda: True)
    monkeypatch.setattr(
        sync_service.SyncService, '_notify_success',
        staticmethod(lambda *a, **k: None),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_notify_error',
        staticmethod(lambda *a, **k: None),
    )
    return sync_service


def _unpaid_order_with_only_order_unsynced(settings):
    from base.models import CashRegister, Order, SyncQueueRecord, User

    user = User.objects.create(
        first_name='Queue', last_name='Race', email='queue-race@example.com',
        password='!', role='CASHIER',
    )
    # The test is about the order slot; keep its required parent out of the
    # reconcile sweep so send_batch is invoked exactly once per push.
    User.objects.filter(pk=user.pk).update(synced_at=timezone.now())
    SyncQueueRecord.objects.filter(
        model_name='user', record_uuid=user.uuid,
    ).delete()
    # Paying the order now takes the branch accounting lock. Seed that owner
    # row as already synchronized so this generation-safety fixture still has
    # exactly one live/queued model under test: Order.
    register = CashRegister.objects.create(
        branch_id=settings.BRANCH_ID, current_balance=0,
    )
    CashRegister.objects.filter(pk=register.pk).update(
        synced_at=timezone.now(),
    )
    SyncQueueRecord.objects.filter(
        model_name='cashregister', record_uuid=register.uuid,
    ).delete()
    return Order.objects.create(
        user=user, cashier=user, status='READY', is_paid=False,
        subtotal='50000.00', total_amount='50000.00',
    )


class TestPushQueueGenerationSafety:
    def test_edit_during_old_send_stays_unsynced_when_on_save_is_off(
        self, settings, monkeypatch,
    ):
        """An old ACK must not stamp a newer live row as delivered.

        This is the production payment-loss race: the unpaid payload is already
        in flight, the cashier pays while SYNC_ON_SAVE=False, and the response
        for the old version arrives afterwards.
        """
        from base.models import Order, SyncQueueRecord
        from base.services.sync.queue import SyncQueue

        settings.SYNC_ON_SAVE = False
        sync_service = _configure_local_push(settings, monkeypatch)
        order = _unpaid_order_with_only_order_unsynced(settings)
        SyncQueue.add('order', order.uuid, order.to_sync_dict())

        sent_payloads = []

        def fake_send_batch(model_name, records, retry=True):
            assert model_name == 'order'
            sent_payloads.append(records[0].copy())
            if len(sent_payloads) == 1:
                assert records[0]['is_paid'] is False
                current = Order.objects.get(pk=order.pk)
                current.is_paid = True
                current.payment_method = 'CASH'
                current.paid_at = timezone.now()
                current.save(update_fields=[
                    'is_paid', 'payment_method', 'paid_at',
                ])
            return {
                'success': True, 'created': 1, 'updated': 0, 'skipped': 0,
                'errors': [], 'failed_uuids': [],
                'acknowledged_uuids': [
                    str(record['uuid']) for record in records
                ],
                'retryable_uuids': [],
                'rejected_uuids': [],
            }

        monkeypatch.setattr(sync_service, 'send_batch', fake_send_batch)

        first = sync_service.SyncService.push()
        order.refresh_from_db()
        assert first['success'] is True
        assert order.is_paid is True
        assert order.synced_at is None, 'old ACK must not stamp the newer version'
        queued = SyncQueueRecord.objects.get(
            model_name='order', record_uuid=order.uuid,
        )
        assert queued.payload['is_paid'] is True
        assert queued.payload['sync_version'] == order.sync_version

        # The newer durable generation is already queued in the same save
        # transaction; the next push delivers it rather than rebuilding it from
        # a best-effort sweep.
        second = sync_service.SyncService.push()
        order.refresh_from_db()
        assert second['success'] is True
        assert sent_payloads[1]['is_paid'] is True
        assert sent_payloads[1]['sync_version'] == order.sync_version
        assert order.synced_at is not None

    def test_old_ack_cannot_delete_on_save_replacement(
        self, settings, monkeypatch, django_capture_on_commit_callbacks,
    ):
        """A save callback that rotates the queue generation wins over old ACK."""
        from base.models import Order, SyncQueueRecord
        from base.services.sync.queue import SyncQueue

        settings.SYNC_ON_SAVE = False
        sync_service = _configure_local_push(settings, monkeypatch)
        order = _unpaid_order_with_only_order_unsynced(settings)
        SyncQueue.add('order', order.uuid, order.to_sync_dict())
        old_generation = SyncQueueRecord.objects.get(
            model_name='order', record_uuid=order.uuid,
        ).generation
        settings.SYNC_ON_SAVE = True

        def fake_send_batch(model_name, records, retry=True):
            assert records[0]['is_paid'] is False
            current = Order.objects.get(pk=order.pk)
            current.is_paid = True
            current.payment_method = 'CASH'
            current.paid_at = timezone.now()
            # Execute the on_commit queue callback inside the test's outer DB
            # transaction so this deterministically replaces the in-flight slot.
            with django_capture_on_commit_callbacks(execute=True):
                current.save(update_fields=[
                    'is_paid', 'payment_method', 'paid_at',
                ])
            return {
                'success': True, 'created': 1, 'updated': 0, 'skipped': 0,
                'errors': [], 'failed_uuids': [],
                'acknowledged_uuids': [
                    str(record['uuid']) for record in records
                ],
                'retryable_uuids': [],
                'rejected_uuids': [],
            }

        monkeypatch.setattr(sync_service, 'send_batch', fake_send_batch)
        result = sync_service.SyncService.push()

        queued = SyncQueueRecord.objects.get(
            model_name='order', record_uuid=order.uuid,
        )
        order.refresh_from_db()
        assert result['success'] is True
        assert queued.generation != old_generation
        assert queued.payload['is_paid'] is True
        assert queued.payload['sync_version'] == order.sync_version
        assert queued.attempts == 0
        assert order.synced_at is None

    def test_changed_dead_letter_payload_rotates_and_revives(self, settings):
        from base.models import SyncQueueRecord
        from base.services.sync.queue import SyncQueue

        settings.SYNC_MAX_QUEUE_ATTEMPTS = 2
        record_uuid = uuidlib.uuid4()
        old = {'uuid': str(record_uuid), 'sync_version': 1, 'name': 'bad'}
        SyncQueue.add('product', record_uuid, old)
        row = SyncQueueRecord.objects.get(
            model_name='product', record_uuid=record_uuid,
        )
        old_generation = row.generation
        SyncQueueRecord.objects.filter(pk=row.pk).update(
            attempts=2, last_error='rejected',
        )

        # The periodic reconcile sweep re-adding identical poison content must
        # preserve the cap; otherwise dead-lettering can never stick.
        SyncQueue.add('product', record_uuid, old)
        row.refresh_from_db()
        assert row.attempts == 2
        assert row.generation == old_generation
        assert 'product' not in SyncQueue.get_grouped()

        corrected = {**old, 'sync_version': 2, 'name': 'fixed'}
        SyncQueue.add('product', record_uuid, corrected)
        row.refresh_from_db()
        assert row.attempts == 0
        assert row.last_error == ''
        assert row.generation != old_generation
        # A delayed failure response for the dead generation must not consume a
        # retry attempt from the corrected payload either.
        SyncQueue.mark_batch_failed(
            [str(record_uuid)], 'late failure', model_name='product',
            generations={str(record_uuid): str(old_generation)},
        )
        row.refresh_from_db()
        assert row.attempts == 0
        assert row.last_error == ''
        assert SyncQueue.get_grouped()['product'][0]['data'] == corrected

    def test_late_stale_reconcile_cannot_replace_newer_queued_version(self):
        from base.models import SyncQueueRecord
        from base.services.sync.queue import SyncQueue

        record_uuid = uuidlib.uuid4()
        newer = {
            'uuid': str(record_uuid), 'sync_version': 9, 'name': 'newer',
        }
        stale = {
            'uuid': str(record_uuid), 'sync_version': 8, 'name': 'stale',
        }
        SyncQueue.add('product', record_uuid, newer)
        row = SyncQueueRecord.objects.get(
            model_name='product', record_uuid=record_uuid,
        )
        generation = row.generation

        SyncQueue.add('product', record_uuid, stale)

        row.refresh_from_db()
        assert row.generation == generation
        assert row.payload == newer


def test_reconcile_poison_row_does_not_block_later_unsynced_siblings(
    settings, monkeypatch,
):
    """A single unserializable legacy row cannot strand a whole model."""
    from base.models import Category, SyncQueueRecord
    from base.services.sync import service as sync_service

    settings.SYNC_ENABLED = True
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'main'

    first = Category.objects.create(name='Poison first', branch_id='main')
    second = Category.objects.create(name='Healthy second', branch_id='main')
    Category.objects.filter(pk__in=[first.pk, second.pk]).update(synced_at=None)
    SyncQueueRecord.objects.all().delete()

    monkeypatch.setattr(sync_service, 'SYNC_ORDER', ['category'])
    monkeypatch.setattr(
        sync_service, 'get_all_models', lambda: {'category': Category},
    )
    original = Category.to_sync_dict

    def one_poison(self):
        if self.pk == first.pk:
            raise ValueError('legacy field cannot be serialized')
        return original(self)

    monkeypatch.setattr(Category, 'to_sync_dict', one_poison)

    requeued = sync_service.SyncService._reconcile_unsynced()

    assert requeued == 1
    assert not SyncQueueRecord.objects.filter(
        model_name='category', record_uuid=first.uuid,
    ).exists()
    queued = SyncQueueRecord.objects.get(
        model_name='category', record_uuid=second.uuid,
    )
    assert queued.payload['uuid'] == str(second.uuid)
