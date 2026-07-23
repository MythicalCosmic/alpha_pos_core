import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from django.test import RequestFactory
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_receive_ack_partition_is_complete_and_disjoint(monkeypatch, settings):
    from base.services.sync.receiver import (
        CloudReceiver,
        RetryableSyncError,
        _rejected,
    )

    settings.DEPLOYMENT_MODE = 'local'
    good, deferred, rejected = [str(uuid4()) for _ in range(3)]

    def apply(cls, model_class, record, branch_id):
        if record['uuid'] == deferred:
            raise RetryableSyncError(
                'parent missing', reason_code='MISSING_DEPENDENCY',
            )
        if record['uuid'] == rejected:
            return _rejected(None, 'STALE_VERSION', 'older than server')
        return None, 'created'

    monkeypatch.setattr(
        CloudReceiver, '_create_or_update', classmethod(apply),
    )
    monkeypatch.setattr(
        CloudReceiver, '_run_periodic_money_reconciliation',
        staticmethod(lambda: []),
    )

    result = CloudReceiver.receive_batch(
        'order',
        'branch-a',
        [{'uuid': value} for value in (good, deferred, rejected)],
    )

    assert result['ack_protocol_version'] == 2
    assert result['acknowledged_uuids'] == [good]
    assert result['retryable_uuids'] == [deferred]
    assert result['rejected_uuids'] == [rejected]
    partitions = [
        set(result[key]) for key in (
            'acknowledged_uuids', 'retryable_uuids', 'rejected_uuids',
        )
    ]
    assert not partitions[0] & partitions[1]
    assert not partitions[0] & partitions[2]
    assert not partitions[1] & partitions[2]
    assert set().union(*partitions) == {good, deferred, rejected}
    assert set(result['failed_uuids']) == {deferred, rejected}


def test_transport_rejects_incomplete_or_overlapping_ack_partitions(
    settings, monkeypatch,
):
    from base.services.sync import transport

    class Response:
        status_code = 200
        text = ''

        @staticmethod
        def json():
            value = str(record_uuid)
            return {
                'ack_protocol_version': 2,
                'success': True,
                'acknowledged_uuids': [value],
                'retryable_uuids': [value],
                'rejected_uuids': [],
                'errors': [],
            }

    record_uuid = uuid4()
    settings.CLOUD_SYNC_URL = 'https://cloud.test/sync'
    settings.CLOUD_SYNC_TOKEN = 'token'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_MAX_RETRIES = 1
    monkeypatch.setattr(transport.requests, 'post', lambda *a, **k: Response())

    result = transport.send_batch('order', [{'uuid': str(record_uuid)}])

    assert result['success'] is False
    assert result['acknowledged_uuids'] == []
    assert result['retryable_uuids'] == [str(record_uuid)]
    assert 'overlap' in result['error']


def test_higher_cloud_version_rebases_pending_paid_order_generation(
    settings, order_factory, django_capture_on_commit_callbacks,
):
    from base.models import Order, SyncQueueRecord
    from base.services.sync.queue import SyncQueue
    from base.services.sync.service import SyncService

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch1'
    settings.SYNC_ENABLED = True
    settings.SYNC_ON_SAVE = True
    order = order_factory(status='READY')
    action_id = uuid4()
    paid_at = timezone.now()
    Order.objects.filter(pk=order.pk).update(
        is_paid=True,
        payment_action_id=action_id,
        payment_method='CASH',
        paid_at=paid_at,
        sync_version=2,
        synced_at=None,
    )
    order.refresh_from_db()
    SyncQueue.add('order', order.uuid, order.to_sync_dict())

    incoming = order.to_sync_dict()
    incoming.update({
        'sync_version': 3,
        'is_paid': False,
        'payment_action_id': None,
        'payment_method': None,
        'paid_at': None,
        'description': 'cloud operational edit',
        'updated_at': (timezone.now() + timedelta(seconds=1)).isoformat(),
    })
    with django_capture_on_commit_callbacks(execute=True):
        result = SyncService._apply_records(Order, [incoming])

    assert result['errors'] == []
    order.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_action_id == action_id
    assert order.payment_method == 'CASH'
    assert order.sync_version == 4
    assert order.synced_at is None
    queued = SyncQueueRecord.objects.get(
        model_name='order', record_uuid=order.uuid,
    )
    assert queued.payload['sync_version'] == 4
    assert queued.payload['is_paid'] is True
    assert queued.payload['payment_action_id'] == str(action_id)


def test_missing_user_then_order_cluster_lands_without_privilege_escalation(
    settings,
):
    from django.contrib.auth.hashers import is_password_usable
    from base.models import Order, User
    from base.security.hashing import hash_password
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    user_uuid = str(uuid4())
    user_result = CloudReceiver.receive_batch(
        'user',
        'branch-a',
        [{
            'uuid': user_uuid,
            'sync_version': 1,
            'branch_id': 'branch-a',
            'first_name': 'Offline',
            'last_name': 'Cashier',
            'email': 'offline-cluster@example.test',
            'password': hash_password('branch-known'),
            'role': 'ADMIN',
            'status': 'ACTIVE',
            'permissions': ['*'],
        }],
    )

    assert user_result['acknowledged_uuids'] == [user_uuid]
    provisioned = User.objects.get(uuid=user_uuid)
    assert provisioned.role == User.RoleChoices.USER
    assert provisioned.status == User.UserStatus.SUSPENDED
    assert provisioned.permissions == []
    assert is_password_usable(provisioned.password) is False

    order_uuid = str(uuid4())
    order_result = CloudReceiver.receive_batch(
        'order',
        'branch-a',
        [{
            'uuid': order_uuid,
            'sync_version': 1,
            'branch_id': 'branch-a',
            'user_uuid': user_uuid,
            'cashier_uuid': user_uuid,
            'status': 'READY',
            'subtotal': '10.00',
            'total_amount': '10.00',
            'order_origin': 'POS',
        }],
    )

    assert order_result['acknowledged_uuids'] == [order_uuid]
    landed = Order.objects.get(uuid=order_uuid)
    assert landed.user_id == provisioned.pk
    assert landed.cashier_id == provisioned.pk


def test_existing_global_user_is_immutable_and_returns_canonical_alias(settings):
    from base.models import User
    from base.security.hashing import hash_password
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    original_password = hash_password('cloud-only')
    canonical = User.objects.create(
        first_name='Cloud',
        last_name='Admin',
        email='canonical-user@example.test',
        password=original_password,
        role=User.RoleChoices.ADMIN,
        permissions=['reports'],
        branch_id='cloud',
    )
    branch_uuid = str(uuid4())

    result = CloudReceiver.receive_batch(
        'user',
        'branch-a',
        [{
            'uuid': branch_uuid,
            'email': canonical.email,
            'first_name': 'Forged',
            'last_name': 'Identity',
            'password': hash_password('attacker-choice'),
            'role': 'CASHIER',
            'permissions': ['*'],
        }],
    )

    assert result['acknowledged_uuids'] == [branch_uuid]
    evidence = result['record_results'][0]
    assert evidence['canonical_uuid'] == str(canonical.uuid)
    canonical.refresh_from_db()
    assert canonical.role == User.RoleChoices.ADMIN
    assert canonical.password == original_password
    assert canonical.permissions == ['reports']


def test_existing_global_user_uuid_with_wrong_email_is_rejected(settings):
    from base.models import User
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    canonical = User.objects.create(
        first_name='Cloud',
        last_name='Identity',
        email='uuid-owner@example.test',
        password='!',
        role=User.RoleChoices.ADMIN,
        branch_id='cloud',
    )

    result = CloudReceiver.receive_batch(
        'user',
        'branch-a',
        [{
            'uuid': str(canonical.uuid),
            'email': 'attacker-controlled@example.test',
        }],
    )

    assert result['acknowledged_uuids'] == []
    assert result['rejected_uuids'] == [str(canonical.uuid)]
    assert result['record_results'][0]['reason_code'] == (
        'GLOBAL_USER_IDENTITY_MISMATCH'
    )


def test_deleted_global_user_email_gets_distinct_safe_bridge(settings):
    from django.contrib.auth.hashers import is_password_usable
    from base.models import User
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    deleted = User.objects.create(
        first_name='Deleted',
        last_name='Identity',
        email='deleted-identity@example.test',
        password='!',
        is_deleted=True,
        branch_id='cloud',
    )
    incoming_uuid = str(uuid4())

    result = CloudReceiver.receive_batch(
        'user',
        'branch-a',
        [{
            'uuid': incoming_uuid,
            'email': deleted.email,
            'first_name': 'Replacement',
            'last_name': 'Attempt',
        }],
    )

    assert result['acknowledged_uuids'] == [incoming_uuid]
    assert result['retryable_uuids'] == []
    assert result['rejected_uuids'] == []
    bridge = User._base_manager.get(uuid=incoming_uuid)
    assert bridge.pk != deleted.pk
    assert bridge.email == deleted.email
    assert bridge.is_deleted is False
    assert bridge.status == User.UserStatus.SUSPENDED
    assert bridge.role == User.RoleChoices.USER
    assert is_password_usable(bridge.password) is False
    deleted.refresh_from_db()
    assert deleted.is_deleted is True


def test_branch_keyed_cursor_and_push_preflight_quarantine(
    settings, monkeypatch,
):
    from base.models import SyncQueueRecord
    from base.services.sync import service as sync_service
    from base.services.sync.queue import SyncQueue
    from base.services.sync.status import SyncStatus

    settings.DEPLOYMENT_MODE = 'local'
    settings.SYNC_ENABLED = True
    settings.CLOUD_SYNC_URL = 'https://cloud.test'
    settings.CLOUD_SYNC_TOKEN = 'token'
    settings.SYNC_MAX_QUEUE_ATTEMPTS = 3
    settings.BRANCH_ID = 'branch-a'
    SyncStatus.set_cursor('2026-07-20T10:00:00+00:00')
    cursor_a_key = SyncStatus.cursor_key()
    stale_uuid = str(uuid4())
    SyncQueue.add('order', stale_uuid, {
        'uuid': stale_uuid,
        'sync_version': 1,
        'branch_id': 'branch-a',
    })

    settings.BRANCH_ID = 'branch-b'
    assert SyncStatus.cursor_key() != cursor_a_key
    assert SyncStatus.get_cursor() is None
    sent = []
    monkeypatch.setattr(sync_service, 'check_health', lambda: True)
    monkeypatch.setattr(
        sync_service, 'send_batch',
        lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_notify_error',
        staticmethod(lambda *a, **k: None),
    )

    result = sync_service.SyncService.push()

    assert result['success'] is False
    assert sent == []
    row = SyncQueueRecord.objects.get(record_uuid=UUID(stale_uuid))
    assert row.last_error.startswith('[BRANCH_SCOPE]')
    assert SyncQueue.dead_letter_count() == 1


def test_database_lock_expiry_never_allows_old_owner_to_release_new_lease(
    settings,
):
    from base.models import SyncState
    from base.services.sync.service import SyncService
    from base.services.sync.status import SyncStatus

    settings.BRANCH_ID = 'branch-a'
    first = SyncService._acquire_lock('push')
    assert first
    assert SyncService._acquire_lock('push') is None
    key = SyncStatus._branch_state_key('sync_lock_push')
    row = SyncState.objects.get(key=key)
    state = json.loads(row.value)
    state['expires_at'] = (
        timezone.now() - timedelta(seconds=1)
    ).isoformat()
    row.value = json.dumps(state)
    row.save(update_fields=['value', 'updated_at'])

    second = SyncService._acquire_lock('push')
    assert second and second != first
    SyncService._release_lock('push', first)
    assert SyncState.objects.filter(key=key).exists()
    SyncService._release_lock('push', second)
    assert not SyncState.objects.filter(key=key).exists()


def test_mixed_array_receive_is_rejected_before_any_record_applies(
    settings, monkeypatch,
):
    from base.services.sync.receiver import CloudReceiver
    from base.services.sync.views import receive

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_TOKEN_MAP = {'token-a': 'branch-a'}
    called = []
    monkeypatch.setattr(
        CloudReceiver, 'receive_batch',
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    request = RequestFactory().post(
        '/api/sync/receive',
        data=json.dumps([
            {
                'model_name': 'order',
                'data': {'uuid': str(uuid4())},
            },
            {
                'model_name': 'orderpayment',
                'data': {'uuid': str(uuid4())},
            },
        ]),
        content_type='application/json',
        HTTP_AUTHORIZATION='Branch token-a',
        HTTP_X_BRANCH_ID='branch-a',
    )

    response = receive(request)

    assert response.status_code == 400
    assert called == []


def test_changes_promotes_only_bounded_null_slice(
    settings, monkeypatch,
):
    from base.models import Category
    from base.services.sync.views import changes

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_TOKEN_MAP = {'token-a': 'branch-a'}
    categories = [
        Category.objects.create(
            name=f'Null {index}',
            slug=f'null-{index}-{uuid4().hex[:8]}',
            branch_id='cloud',
        )
        for index in range(3)
    ]
    Category.objects.filter(
        pk__in=[category.pk for category in categories],
    ).update(synced_at=None)
    monkeypatch.setattr(
        'base.services.presence.mark_device_live',
        lambda *args, **kwargs: None,
    )
    request = RequestFactory().get(
        '/api/sync/changes?per_page=1',
        HTTP_AUTHORIZATION='Branch token-a',
        HTTP_X_BRANCH_ID='branch-a',
    )

    response = changes(request)
    body = json.loads(response.content)

    assert response.status_code == 200
    assert len(body['data']['category']) == 1
    assert Category.objects.filter(
        pk__in=[category.pk for category in categories],
        synced_at__isnull=True,
    ).count() == 2


def test_legacy_missing_dependency_dead_letter_revives_once(settings):
    from base.models import SyncQueueRecord
    from base.services.sync.queue import SyncQueue

    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_MAX_QUEUE_ATTEMPTS = 2
    record_uuid = str(uuid4())
    SyncQueue.add('orderitem', record_uuid, {
        'uuid': record_uuid,
        'branch_id': 'branch-a',
        'order_uuid': str(uuid4()),
    })
    SyncQueueRecord.objects.filter(record_uuid=record_uuid).update(
        attempts=2,
        last_error='Unresolved required FK: parent has not synced yet',
    )

    assert SyncQueue.revive_legacy_dead_letters() == 1
    row = SyncQueueRecord.objects.get(record_uuid=record_uuid)
    assert row.attempts == 0
    assert row.last_error == ''
    assert SyncQueue.revive_legacy_dead_letters() == 0


def test_reconciliation_failure_moves_applied_record_to_retryable(
    settings, monkeypatch, order_factory,
):
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory(status='READY')
    record_uuid = str(order.uuid)
    monkeypatch.setattr(
        CloudReceiver,
        '_create_or_update',
        classmethod(lambda cls, model, data, branch: (order, 'updated')),
    )
    monkeypatch.setattr(
        CloudReceiver,
        '_reconcile_received_order_money',
        staticmethod(lambda ids: (_ for _ in ()).throw(RuntimeError('db busy'))),
    )
    monkeypatch.setattr(
        CloudReceiver, '_run_periodic_money_reconciliation',
        staticmethod(lambda: []),
    )
    notified = []
    monkeypatch.setattr(
        CloudReceiver,
        '_notify_received_orders',
        staticmethod(lambda ids: notified.extend(ids)),
    )

    result = CloudReceiver.receive_batch(
        'order', 'branch1', [{'uuid': record_uuid}],
    )

    assert result['acknowledged_uuids'] == []
    assert result['retryable_uuids'] == [record_uuid]
    assert result['record_results'][0]['reason_code'] == (
        'POST_RECEIVE_RECONCILIATION_FAILED'
    )
    assert notified == []


def test_fallback_cache_add_honors_expired_ttl(monkeypatch):
    from base.services.sync import cache as sync_cache

    class BrokenCache:
        @staticmethod
        def add(*args, **kwargs):
            raise RuntimeError('cache down')

    sync_cache.safe_delete('ttl-test')
    monkeypatch.setattr(sync_cache, '_cache', lambda: BrokenCache())
    assert sync_cache.safe_add('ttl-test', 'first', 0) is True
    assert sync_cache.safe_add('ttl-test', 'second', 1) is True
    assert sync_cache.safe_get('ttl-test') == 'second'
    sync_cache.safe_delete('ttl-test')


def test_receiver_parses_false_string_and_rejects_ambiguous_boolean(
    settings,
):
    from base.models import Order, User
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    user = User.objects.create(
        first_name='Boolean',
        last_name='Owner',
        email='boolean-owner@example.test',
        password='!',
        branch_id='cloud',
    )
    accepted_uuid = str(uuid4())
    rejected_uuid = str(uuid4())
    common = {
        'sync_version': 1,
        'branch_id': 'branch-a',
        'user_uuid': str(user.uuid),
        'status': 'READY',
        'subtotal': '10.00',
        'total_amount': '10.00',
        'order_origin': 'POS',
    }

    accepted = CloudReceiver.receive_batch(
        'order',
        'branch-a',
        [{'uuid': accepted_uuid, 'is_paid': 'false', **common}],
    )
    rejected = CloudReceiver.receive_batch(
        'order',
        'branch-a',
        [{'uuid': rejected_uuid, 'is_paid': 'not-a-boolean', **common}],
    )

    assert accepted['acknowledged_uuids'] == [accepted_uuid]
    assert Order.objects.get(uuid=accepted_uuid).is_paid is False
    assert rejected['rejected_uuids'] == [rejected_uuid]
    assert rejected['record_results'][0]['reason_code'] == 'INVALID_RECORD'
    assert not Order.objects.filter(uuid=rejected_uuid).exists()


def test_deferred_queue_row_is_visible_as_failed_without_poison_attempt(
    settings,
):
    from base.models import SyncQueueRecord
    from base.services.sync.queue import SyncQueue

    record_uuid = str(uuid4())
    generation = SyncQueue.add('order', record_uuid, {
        'uuid': record_uuid,
        'branch_id': 'branch-a',
        'sync_version': 1,
    })
    SyncQueue.mark_batch_deferred(
        [record_uuid],
        'waiting for parent',
        model_name='order',
        generations={record_uuid: generation},
    )

    row = SyncQueueRecord.objects.get(record_uuid=record_uuid)
    assert row.attempts == 0
    assert row.last_error == 'waiting for parent'
    assert SyncQueue.count() == (1, 1)


def test_rejected_order_does_not_block_independent_cashregister(
    settings, monkeypatch,
):
    from base.models import SyncQueueRecord
    from base.services.sync import service as sync_service
    from base.services.sync.queue import SyncQueue
    from base.services.sync.status import SyncStatus

    settings.SYNC_ENABLED = True
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.CLOUD_SYNC_URL = 'https://cloud.test'
    settings.CLOUD_SYNC_TOKEN = 'token'
    settings.SYNC_MAX_QUEUE_ATTEMPTS = 3
    order_uuid = str(uuid4())
    register_uuid = str(uuid4())
    SyncQueue.add('order', order_uuid, {
        'uuid': order_uuid, 'branch_id': 'branch-a', 'sync_version': 1,
    })
    SyncQueue.add('cashregister', register_uuid, {
        'uuid': register_uuid, 'branch_id': 'branch-a', 'sync_version': 1,
        'current_balance': '0.00',
    })
    calls = []

    def send(model_name, records, retry=True):
        calls.append(model_name)
        values = [str(record['uuid']) for record in records]
        rejected = values if model_name == 'order' else []
        acknowledged = [] if rejected else values
        return {
            'success': True,
            'acknowledged_uuids': acknowledged,
            'retryable_uuids': [],
            'rejected_uuids': rejected,
            'record_results': [],
        }

    monkeypatch.setattr(sync_service, 'check_health', lambda: True)
    monkeypatch.setattr(sync_service, 'send_batch', send)
    monkeypatch.setattr(
        sync_service.SyncService,
        '_reconcile_unsynced',
        classmethod(lambda cls: 0),
    )
    monkeypatch.setattr(
        sync_service.SyncService,
        '_notify_error',
        staticmethod(lambda *args, **kwargs: None),
    )

    result = sync_service.SyncService.push()

    assert calls == ['order', 'cashregister']
    assert result['success'] is False
    assert result['failed'] == 1
    assert SyncStatus.get()['last_failed_count'] == 1
    assert SyncQueueRecord.objects.filter(
        model_name='order', record_uuid=order_uuid,
    ).exists()
    assert not SyncQueueRecord.objects.filter(
        model_name='cashregister', record_uuid=register_uuid,
    ).exists()


def test_user_alias_rekeys_queued_order_before_same_push(
    settings, monkeypatch,
):
    from base.models import Order, SyncQueueRecord, User
    from base.services.sync import service as sync_service
    from base.services.sync.queue import SyncQueue

    settings.SYNC_ENABLED = False
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    old = User.objects.create(
        first_name='Local',
        last_name='Bootstrap',
        email='local-bootstrap@example.test',
        password='!',
        branch_id='branch-a',
    )
    canonical = User.objects.create(
        first_name='Cloud',
        last_name='Canonical',
        email='cloud-canonical@example.test',
        password='!',
        branch_id='cloud',
    )
    order = Order.objects.create(
        user=old,
        cashier=old,
        status='READY',
        subtotal='10.00',
        total_amount='10.00',
        branch_id='branch-a',
    )
    SyncQueueRecord.objects.all().delete()
    User.objects.filter(pk=canonical.pk).update(synced_at=timezone.now())
    settings.SYNC_ENABLED = True
    SyncQueue.add('user', old.uuid, old.to_sync_dict())
    SyncQueue.add('order', order.uuid, order.to_sync_dict())
    settings.CLOUD_SYNC_URL = 'https://cloud.test'
    settings.CLOUD_SYNC_TOKEN = 'token'
    sent = []

    def send(model_name, records, retry=True):
        sent.append((model_name, [dict(record) for record in records]))
        values = [str(record['uuid']) for record in records]
        evidence = []
        if model_name == 'user':
            evidence = [{
                'uuid': str(old.uuid),
                'canonical_uuid': str(canonical.uuid),
            }]
        return {
            'success': True,
            'acknowledged_uuids': values,
            'retryable_uuids': [],
            'rejected_uuids': [],
            'record_results': evidence,
        }

    monkeypatch.setattr(sync_service, 'check_health', lambda: True)
    monkeypatch.setattr(sync_service, 'send_batch', send)
    monkeypatch.setattr(
        sync_service.SyncService,
        '_notify_success',
        staticmethod(lambda *args, **kwargs: None),
    )

    result = sync_service.SyncService.push()

    assert result['success'] is True
    order_payload = next(
        records[0] for name, records in sent if name == 'order'
    )
    assert order_payload['user_uuid'] == str(canonical.uuid)
    assert order_payload['cashier_uuid'] == str(canonical.uuid)
    order.refresh_from_db()
    old.refresh_from_db()
    assert order.user_id == canonical.pk
    assert order.cashier_id == canonical.pk
    assert old.is_deleted is True
    assert not SyncQueueRecord.objects.exists()


def test_lock_ttl_covers_full_transport_retry_envelope(settings):
    from base.services.sync.service import _lease_ttl

    settings.SYNC_MAX_RETRIES = 5
    settings.SYNC_TIMEOUT = 60

    # 5 request timeouts + backoffs (1+2+4+8) + the scheduling margin.
    assert _lease_ttl() >= (5 * 60) + 15 + 60


def test_exact_action_identified_payment_replay_is_acknowledged(
    settings, order_factory,
):
    from base.models import Order, OrderPayment
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    order = order_factory(status='READY')
    action_id = uuid4()
    Order.objects.filter(pk=order.pk).update(
        payment_action_id=action_id,
    )
    order.refresh_from_db()
    payment_uuid = str(uuid4())
    payload = {
        'uuid': payment_uuid,
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': order.branch_id,
        'order_uuid': str(order.uuid),
        'method': 'CASH',
        'amount': str(order.total_amount),
        'payment_action_id': str(action_id),
        'line_index': 0,
        'created_at': timezone.now().isoformat(),
    }

    first = CloudReceiver.receive_batch(
        'orderpayment', order.branch_id, [payload],
    )
    replay = CloudReceiver.receive_batch(
        'orderpayment', order.branch_id, [payload],
    )

    assert first['acknowledged_uuids'] == [payment_uuid]
    assert replay['acknowledged_uuids'] == [payment_uuid]
    assert replay['rejected_uuids'] == []
    assert replay['errors'] == []
    assert replay['record_results'][0]['reason_code'] == (
        'IDEMPOTENT_APPEND_ONLY_REPLAY'
    )
    assert OrderPayment.objects.filter(uuid=payment_uuid).count() == 1


def test_legacy_user_alias_keeps_old_fk_uuid_resolvable(settings):
    from base.models import Order, User
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    canonical = User.objects.create(
        first_name='Canonical',
        last_name='Cashier',
        email='rolling-alias@example.test',
        password='!',
        role=User.RoleChoices.CASHIER,
        branch_id='cloud',
    )
    legacy_uuid = str(uuid4())

    user_result = CloudReceiver.receive_batch(
        'user',
        'branch-a',
        [{
            'uuid': legacy_uuid,
            'sync_version': 1,
            'branch_id': 'branch-a',
            'first_name': 'Local',
            'last_name': 'Bootstrap',
            'email': canonical.email,
            'password': 'branch-hash',
            'role': 'CASHIER',
            'status': 'ACTIVE',
        }],
        client_ack_protocol=1,
    )

    assert user_result['acknowledged_uuids'] == [legacy_uuid]
    assert user_result['failed_uuids'] == []
    assert user_result['errors'] == []
    assert 'canonical_uuid' not in user_result['record_results'][0]

    order_uuid = str(uuid4())
    order_result = CloudReceiver.receive_batch(
        'order',
        'branch-a',
        [{
            'uuid': order_uuid,
            'sync_version': 1,
            'branch_id': 'branch-a',
            'user_uuid': legacy_uuid,
            'cashier_uuid': legacy_uuid,
            'status': 'READY',
            'subtotal': '10.00',
            'total_amount': '10.00',
            'order_origin': 'POS',
        }],
        client_ack_protocol=1,
    )

    assert order_result['acknowledged_uuids'] == [order_uuid]
    landed = Order.objects.get(uuid=order_uuid)
    assert landed.user_id == canonical.pk
    assert landed.cashier_id == canonical.pk


def test_stale_user_queue_ack_is_idempotent_after_pull_rekey(
    settings, monkeypatch,
):
    from base.models import Order, SyncQueueRecord, User
    from base.services.sync import service as sync_service
    from base.services.sync.queue import SyncQueue

    settings.SYNC_ENABLED = False
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_MAX_QUEUE_ATTEMPTS = 3
    old = User.objects.create(
        first_name='Local',
        last_name='Bootstrap',
        email='already-rekeyed@example.test',
        password='!',
        branch_id='branch-a',
    )
    order = Order.objects.create(
        user=old,
        cashier=old,
        status='READY',
        subtotal='10.00',
        total_amount='10.00',
        branch_id='branch-a',
    )
    old_uuid = str(old.uuid)
    canonical_uuid = str(uuid4())
    SyncQueue.add('user', old_uuid, old.to_sync_dict())
    SyncQueue.add('order', order.uuid, order.to_sync_dict())

    # Simulate the cloud pull's natural-key convergence happening before the
    # stale User queue generation receives its push acknowledgement.
    User.objects.filter(pk=old.pk).update(
        uuid=canonical_uuid,
        branch_id='cloud',
        synced_at=timezone.now(),
    )
    settings.SYNC_ENABLED = True
    settings.CLOUD_SYNC_URL = 'https://cloud.test'
    settings.CLOUD_SYNC_TOKEN = 'token'
    sent = []

    def send(model_name, records, retry=True):
        snapshots = [dict(record) for record in records]
        sent.append((model_name, snapshots))
        values = [str(record['uuid']) for record in records]
        evidence = []
        if model_name == 'user':
            evidence = [{
                'uuid': old_uuid,
                'canonical_uuid': canonical_uuid,
            }]
        return {
            'success': True,
            'acknowledged_uuids': values,
            'retryable_uuids': [],
            'rejected_uuids': [],
            'record_results': evidence,
        }

    monkeypatch.setattr(sync_service, 'check_health', lambda: True)
    monkeypatch.setattr(sync_service, 'send_batch', send)
    monkeypatch.setattr(
        sync_service.SyncService,
        '_notify_success',
        staticmethod(lambda *args, **kwargs: None),
    )

    result = sync_service.SyncService.push()

    assert result['success'] is True
    assert result['identity_aliases'] == {old_uuid: canonical_uuid}
    order_payload = next(
        records[0] for name, records in sent if name == 'order'
    )
    assert order_payload['user_uuid'] == canonical_uuid
    assert order_payload['cashier_uuid'] == canonical_uuid
    assert not SyncQueueRecord.objects.exists()


def test_transport_advertises_ack_protocol_v2(settings, monkeypatch):
    from base.services.sync import transport

    settings.CLOUD_SYNC_TOKEN = 'token'
    settings.BRANCH_ID = 'branch-a'
    monkeypatch.setattr(
        'base.services.presence.device_presence_headers',
        lambda: {},
    )

    assert transport._auth_headers()['X-Sync-Ack-Protocol'] == '2'


def test_legacy_user_aliases_are_isolated_by_authenticated_branch(settings):
    from base.models import Order, User
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'cloud'
    canonical_a = User.objects.create(
        first_name='Branch A',
        last_name='Cashier',
        email='alias-a@example.test',
        password='!',
        role=User.RoleChoices.CASHIER,
        branch_id='cloud',
    )
    canonical_b = User.objects.create(
        first_name='Branch B',
        last_name='Cashier',
        email='alias-b@example.test',
        password='!',
        role=User.RoleChoices.CASHIER,
        branch_id='cloud',
    )
    shared_legacy_uuid = str(uuid4())

    for branch, canonical in (
        ('branch-a', canonical_a),
        ('branch-b', canonical_b),
    ):
        result = CloudReceiver.receive_batch(
            'user',
            branch,
            [{
                'uuid': shared_legacy_uuid,
                'email': canonical.email,
                'first_name': branch,
                'last_name': 'Local',
            }],
            client_ack_protocol=1,
        )
        assert result['acknowledged_uuids'] == [shared_legacy_uuid]

    landed = {}
    for branch in ('branch-a', 'branch-b'):
        order_uuid = str(uuid4())
        result = CloudReceiver.receive_batch(
            'order',
            branch,
            [{
                'uuid': order_uuid,
                'sync_version': 1,
                'branch_id': branch,
                'user_uuid': shared_legacy_uuid,
                'cashier_uuid': shared_legacy_uuid,
                'status': 'READY',
                'subtotal': '10.00',
                'total_amount': '10.00',
                'order_origin': 'POS',
            }],
            client_ack_protocol=1,
        )
        assert result['acknowledged_uuids'] == [order_uuid]
        landed[branch] = Order.objects.get(uuid=order_uuid)

    assert landed['branch-a'].user_id == canonical_a.pk
    assert landed['branch-a'].cashier_id == canonical_a.pk
    assert landed['branch-b'].user_id == canonical_b.pk
    assert landed['branch-b'].cashier_id == canonical_b.pk

    User.objects.filter(pk=canonical_a.pk).update(is_deleted=True)
    blocked_uuid = str(uuid4())
    blocked = CloudReceiver.receive_batch(
        'order',
        'branch-a',
        [{
            'uuid': blocked_uuid,
            'sync_version': 1,
            'branch_id': 'branch-a',
            'user_uuid': shared_legacy_uuid,
            'status': 'READY',
            'subtotal': '10.00',
            'total_amount': '10.00',
            'order_origin': 'POS',
        }],
        client_ack_protocol=1,
    )
    assert blocked['retryable_uuids'] == [blocked_uuid]
    assert not Order.objects.filter(uuid=blocked_uuid).exists()
