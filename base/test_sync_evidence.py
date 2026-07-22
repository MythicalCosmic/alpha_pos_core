import uuid

import pytest

from base.services.sync.evidence import (
    emit_sync_evidence,
    register_sync_evidence_observer,
    unregister_sync_evidence_observer,
)


def test_observer_failure_can_never_break_sync():
    def broken(_event_type, _payload):
        raise RuntimeError('evidence disk unavailable')

    register_sync_evidence_observer(broken)
    try:
        emit_sync_evidence('test', value=1)
    finally:
        unregister_sync_evidence_observer(broken)


@pytest.mark.django_db(transaction=True)
def test_queue_emits_exact_generation_payload_failure_and_acknowledgement():
    from base.services.sync.queue import SyncQueue

    events = []
    def observer(event_type, payload):
        events.append((event_type, payload))
    register_sync_evidence_observer(observer)
    record_uuid = uuid.uuid4()
    payload = {'uuid': str(record_uuid), 'total_amount': '639400', 'sync_version': 1}
    try:
        generation = SyncQueue.add('order', record_uuid, payload)
        queued = SyncQueue.get_grouped()['order']
        assert queued[0]['generation'] == generation
        assert queued[0]['data'] == payload

        SyncQueue.mark_batch_failed(
            [str(record_uuid)], 'offline', model_name='order',
            generations={str(record_uuid): generation},
        )
        assert SyncQueue.acknowledge(SyncQueue.get_grouped()['order'], 'order') == {
            str(record_uuid),
        }
    finally:
        unregister_sync_evidence_observer(observer)

    names = [name for name, _payload in events]
    assert names == ['queue_upsert', 'queue_failed', 'queue_acknowledged']
    assert events[0][1]['record']['data'] == payload
    assert events[1][1]['records'][0]['attempts'] == 1
    assert events[2][1]['records'][0]['generation'] == generation


def test_transport_emits_actual_attempt_payload_and_receiver_response(monkeypatch):
    from base.services.sync import transport

    monkeypatch.setattr(transport, 'get_cloud_url', lambda: 'https://cloud.example/api')
    monkeypatch.setattr(transport, 'get_cloud_token', lambda: 'never-exported')
    monkeypatch.setattr(transport, 'get_branch_id', lambda: 'branch-1')
    monkeypatch.setattr(transport, 'get_sync_timeout', lambda: 3)
    monkeypatch.setattr(transport, 'get_sync_max_retries', lambda: 2)
    monkeypatch.setattr(transport, 'get_sync_require_https', lambda: True)

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {'success': True, 'created': 1, 'failed_uuids': []}

    monkeypatch.setattr(transport.requests, 'post', lambda *args, **kwargs: Response())
    events = []
    def observer(event_type, payload):
        events.append((event_type, payload))
    register_sync_evidence_observer(observer)
    records = [{'uuid': str(uuid.uuid4()), 'total_amount': '639400'}]
    try:
        assert transport.send_batch('order', records)['success'] is True
    finally:
        unregister_sync_evidence_observer(observer)

    assert [name for name, _payload in events] == [
        'push_http_attempt', 'push_http_response',
    ]
    assert events[0][1]['records'] == records
    assert events[0][1]['payload_sha256']
    assert events[1][1]['http_status'] == 200
    assert 'never-exported' not in repr(events)
