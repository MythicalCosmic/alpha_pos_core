"""Regression coverage for the one-way central audit trail."""

import json
from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _actor():
    from base.models import User

    return User.objects.create(
        first_name='Audit', last_name='Actor',
        email='sync-audit@example.com', password='!', role='CASHIER',
        branch_id='branch1',
    )


def test_authenticated_branch_push_creates_append_only_cloud_audit(settings):
    from base.models import AuditLog
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    actor = _actor()
    occurred_at = timezone.now() - timedelta(days=1)
    local = AuditLog(
        actor=actor, action=AuditLog.Action.ORDER_CANCEL,
        target_type='Order', target_id=42,
        metadata={'reason': 'customer request'}, ip_address='127.0.0.1',
        branch_id='spoofed-branch',
    )
    payload = local.to_sync_dict()
    payload['created_at'] = occurred_at.isoformat()

    result = CloudReceiver.receive_batch('auditlog', 'branch1', [payload])

    assert result['created'] == 1, result
    stored = AuditLog.objects.get(uuid=local.uuid)
    assert stored.branch_id == 'branch1'
    assert stored.actor_id == actor.id
    assert stored.action == AuditLog.Action.ORDER_CANCEL
    assert stored.metadata == {'reason': 'customer request'}
    assert abs((stored.created_at - occurred_at).total_seconds()) < 1

    # Replaying or forging a higher version cannot rewrite append-only history.
    payload['sync_version'] = 999
    payload['action'] = AuditLog.Action.TREASURY_TRANSFER
    payload['metadata'] = {'forged': True}
    replay = CloudReceiver.receive_batch('auditlog', 'branch1', [payload])
    stored.refresh_from_db()
    assert replay['skipped'] == 1
    assert stored.action == AuditLog.Action.ORDER_CANCEL
    assert stored.metadata == {'reason': 'customer request'}


def test_audit_log_is_registered_for_reconcile_but_never_in_pull_feed(
    settings, monkeypatch,
):
    from base.models import AuditLog, SyncQueueRecord
    from base.services.sync import views
    from base.services.sync.config import MODEL_MAP, SYNC_ORDER
    from base.services.sync.service import SyncService

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch1'
    settings.SYNC_ENABLED = True
    settings.SYNC_ON_SAVE = False
    actor = _actor()
    audit = AuditLog.objects.create(
        actor=actor, action=AuditLog.Action.ORDER_CANCEL,
        target_type='Order', target_id=7,
    )

    assert 'auditlog' in SYNC_ORDER
    assert MODEL_MAP['auditlog'] == 'base.AuditLog'
    SyncService._reconcile_unsynced()
    assert SyncQueueRecord.objects.filter(
        model_name='auditlog', record_uuid=audit.uuid,
    ).exists()

    # Even a cloud-owned/different-branch event is one-way and must never be
    # exposed to a terminal's change feed.
    AuditLog.objects.filter(pk=audit.pk).update(
        branch_id='branch2', synced_at=timezone.now(),
    )
    settings.ALLOWED_BRANCH_TOKENS = ['audit-feed-token']
    settings.ALLOWED_BRANCH_IDS = ['branch1']
    settings.BRANCH_TOKEN_MAP = {}
    request = RequestFactory().get(
        '/api/sync/changes',
        HTTP_AUTHORIZATION='Branch audit-feed-token',
        HTTP_X_BRANCH_ID='branch1',
    )
    response = views.changes(request)
    body = json.loads(response.content)

    assert response.status_code == 200
    assert 'auditlog' not in body['data']
