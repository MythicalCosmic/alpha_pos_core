"""A pushed batch is answered within the receive time budget."""

import pytest


pytestmark = pytest.mark.django_db


def _audit_payloads(count):
    from base.models import AuditLog, User

    actor = User.objects.create(
        first_name='Budget', last_name='Actor', email='budget@example.com',
        password='!', role='CASHIER', branch_id='branch1',
    )
    return [
        AuditLog(
            actor=actor, action=AuditLog.Action.ORDER_CANCEL, target_type='Order',
            target_id=index, metadata={}, branch_id='branch1',
        ).to_sync_dict()
        for index in range(count)
    ]


def test_records_past_the_budget_are_retryable_and_apply_on_resend(settings, monkeypatch):
    from base.models import AuditLog
    from base.services.sync import receiver
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.SYNC_RECEIVE_TIME_BUDGET_SECONDS = 10
    payloads = _audit_payloads(4)
    uuids = [str(p['uuid']) for p in payloads]
    # Clock reads: batch start 0 s, before record 2 at 6 s (fits), before record 3 at 12 s (over).
    clock = iter([0, 6, 12, 99])
    monkeypatch.setattr(receiver.time, 'monotonic', lambda: next(clock))

    result = CloudReceiver.receive_batch('auditlog', 'branch1', payloads)

    assert result['acknowledged_uuids'] == uuids[:2]
    assert result['retryable_uuids'] == uuids[2:]
    assert result['rejected_uuids'] == []
    deferred = [r for r in result['record_results'] if r['uuid'] in uuids[2:]]
    assert {r['reason_code'] for r in deferred} == {'RECEIVE_TIME_BUDGET'}
    assert AuditLog.objects.filter(uuid__in=uuids[:2]).count() == 2
    assert not AuditLog.objects.filter(uuid__in=uuids[2:]).exists()

    # The till resends only the retryable records; with time to spare they apply.
    monkeypatch.setattr(receiver.time, 'monotonic', lambda: 0)
    resend = CloudReceiver.receive_batch('auditlog', 'branch1', payloads[2:])
    assert resend['acknowledged_uuids'] == uuids[2:]
    assert AuditLog.objects.filter(uuid__in=uuids).count() == 4


def test_first_record_always_applies_and_zero_disables_the_budget(settings, monkeypatch):
    from base.services.sync import receiver
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.SYNC_RECEIVE_TIME_BUDGET_SECONDS = 1
    payloads = _audit_payloads(2)
    clock = iter([0, 50, 50, 50])
    monkeypatch.setattr(receiver.time, 'monotonic', lambda: next(clock))
    result = CloudReceiver.receive_batch('auditlog', 'branch1', payloads)
    assert result['acknowledged_uuids'] == [str(payloads[0]['uuid'])]
    assert result['retryable_uuids'] == [str(payloads[1]['uuid'])]

    settings.SYNC_RECEIVE_TIME_BUDGET_SECONDS = 0
    monkeypatch.setattr(receiver.time, 'monotonic', lambda: 1e9)
    again = CloudReceiver.receive_batch('auditlog', 'branch1', payloads[1:])
    assert again['acknowledged_uuids'] == [str(payloads[1]['uuid'])]
