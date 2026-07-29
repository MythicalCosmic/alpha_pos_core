import json
from datetime import timedelta

import pytest
from django.utils import timezone

from base.models import CashReconciliation, Order, OrderPayment, Shift, User
from base.services.sync.receiver import CloudReceiver
from core.shifts.service import (
    ShiftService,
    settlement_manifest_digest,
)


BRANCH = 'branch-a'


def _user(email, role='CASHIER'):
    return User.objects.create(
        first_name='Shift',
        last_name='Tester',
        email=email,
        password='!',
        role=role,
        status='ACTIVE',
        branch_id=BRANCH,
    )


def _closed_shift(*, with_expense=False):
    cashier = _user('close-cashier@example.test')
    shift = Shift.objects.create(
        user=cashier,
        branch_id=BRANCH,
        start_time=timezone.now() - timedelta(hours=2),
        status=Shift.Status.ACTIVE,
        treasury_settlement_eligible=True,
    )
    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        branch_id=BRANCH,
        status=Order.Status.READY,
        is_paid=True,
        subtotal='127.00',
        total_amount='127.00',
        paid_at=timezone.now() - timedelta(minutes=5),
        payment_method=Order.PaymentMethod.CASH,
    )
    OrderPayment.objects.create(
        order=order,
        branch_id=BRANCH,
        method=Order.PaymentMethod.CASH,
        amount='127.00',
    )
    if with_expense:
        from cashbox.models import CashboxExpense
        CashboxExpense.objects.create(
            shift=shift,
            branch_id=BRANCH,
            amount='7.00',
            comment='drawer expense still in the next sync batch',
            created_by=cashier,
        )
    response, status = ShiftService.end_shift(
        shift.id,
        cashier.id,
        notes='immutable close',
        actor=cashier,
        counted={'CASH': '127.00'},
    )
    assert status == 200, response
    shift.refresh_from_db()
    return cashier, shift, order


def _ack_payload(shift):
    manifest = shift.settlement_manifest
    return {
        'shift_uuid': str(shift.uuid),
        'manifest_version': manifest['version'],
        'manifest_digest': settlement_manifest_digest(manifest),
    }


def _post_ack(client, settings, payload, *, token='branch-token', branch=BRANCH):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_TOKEN_MAP = {token: branch}
    return client.post(
        '/api/sync/shift-close/ack',
        data=json.dumps(payload),
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Branch {token}',
        HTTP_X_BRANCH_ID=branch,
    )


@pytest.mark.django_db
def test_stale_manifest_close_is_stored_then_duplicate_is_idempotent(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    _, shift, _ = _closed_shift()
    payload = shift.to_sync_dict()
    incoming_version = payload['sync_version']
    cloud_version = incoming_version + 7
    Shift.objects.filter(pk=shift.pk).update(
        status=Shift.Status.ACTIVE,
        end_time=None,
        total_orders=0,
        total_revenue='0.00',
        cash_collected='0.00',
        settlement_manifest={},
        sync_version=cloud_version,
    )

    first = CloudReceiver.receive_batch('shift', BRANCH, [payload])

    assert first['failed_uuids'] == []
    assert first['updated'] == 1
    assert first['record_results'][0]['state'] == 'STORED'
    shift.refresh_from_db()
    assert shift.status == Shift.Status.ENDED
    assert shift.settlement_manifest == payload['settlement_manifest']
    assert shift.sync_version == cloud_version + 1

    stored_version = shift.sync_version
    replay = CloudReceiver.receive_batch('shift', BRANCH, [payload])

    assert replay['failed_uuids'] == []
    assert replay['skipped'] == 1
    assert replay['record_results'][0]['state'] == 'STORED'
    shift.refresh_from_db()
    assert shift.sync_version == stored_version
    assert shift.status == Shift.Status.ENDED


@pytest.mark.django_db
def test_legacy_close_without_manifest_is_retained_as_critical_conflict(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    _, shift, _ = _closed_shift()
    payload = shift.to_sync_dict()
    payload['settlement_manifest'] = {}
    Shift.objects.filter(pk=shift.pk).update(
        status=Shift.Status.ACTIVE,
        end_time=None,
        total_orders=0,
        total_revenue='0.00',
        cash_collected='0.00',
        settlement_manifest={},
        sync_version=payload['sync_version'] + 2,
    )

    result = CloudReceiver.receive_batch('shift', BRANCH, [payload])

    assert result['failed_uuids'] == [str(shift.uuid)]
    assert result['record_results'][0]['state'] == 'CONFLICT'
    assert result['record_results'][0]['reason_code'] == 'MANIFEST_REQUIRED'
    shift.refresh_from_db()
    assert shift.status == Shift.Status.ACTIVE
    assert shift.end_time is None


@pytest.mark.django_db
def test_close_ack_requires_exact_complete_canonical_bundle(client, settings):
    _, shift, order = _closed_shift()
    payload = _ack_payload(shift)

    acknowledged = _post_ack(client, settings, payload)

    assert acknowledged.status_code == 200
    body = acknowledged.json()
    assert body['state'] == 'ACKNOWLEDGED'
    assert body['acknowledged'] is True
    assert body['manifest_digest'] == payload['manifest_digest']
    assert body['settlement_rows'] == {'expected': 5, 'received': 5}

    OrderPayment.objects.filter(order=order).update(amount='126.00')
    conflict = _post_ack(client, settings, payload)

    assert conflict.status_code == 200
    assert conflict.json()['state'] == 'CONFLICT'
    assert conflict.json()['reason_code'] == 'SETTLEMENT_BUNDLE_CONFLICT'


@pytest.mark.django_db
def test_close_ack_is_pending_until_every_manifest_tender_arrives(client, settings):
    from cashbox.models import ShiftPaymentTotal

    _, shift, _ = _closed_shift()
    payload = _ack_payload(shift)
    ShiftPaymentTotal.objects.filter(shift=shift, method='PAYME').delete()

    response = _post_ack(client, settings, payload)

    assert response.status_code == 200
    body = response.json()
    assert body['state'] == 'PENDING'
    assert body['acknowledged'] is False
    assert body['reason_code'] == 'SETTLEMENT_ROWS_PENDING'
    assert body['settlement_rows'] == {'expected': 5, 'received': 4}


@pytest.mark.django_db
def test_close_ack_stays_pending_while_later_expense_batch_is_missing(client, settings):
    from cashbox.models import CashboxExpense

    _, shift, _ = _closed_shift(with_expense=True)
    payload = _ack_payload(shift)
    CashboxExpense.objects.filter(shift=shift).delete()

    response = _post_ack(client, settings, payload)

    assert response.status_code == 200
    body = response.json()
    assert body['state'] == 'PENDING'
    assert body['reason_code'] == 'EVIDENCE_ROWS_PENDING'
    assert 'expenses' in body['reason']


@pytest.mark.django_db
def test_close_ack_does_not_leak_another_branch_shift(client, settings):
    _, shift, _ = _closed_shift()
    payload = _ack_payload(shift)

    response = _post_ack(
        client,
        settings,
        payload,
        token='other-token',
        branch='branch-b',
    )

    assert response.status_code == 200
    assert response.json()['state'] == 'PENDING'
    assert response.json()['reason_code'] == 'SHIFT_NOT_RECEIVED'


@pytest.mark.django_db
def test_close_ack_get_uses_same_branch_bound_contract(client, settings):
    _, shift, _ = _closed_shift()
    payload = _ack_payload(shift)
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_TOKEN_MAP = {'branch-token': BRANCH}

    response = client.get(
        '/api/sync/shift-close/ack',
        data=payload,
        HTTP_AUTHORIZATION='Branch branch-token',
        HTTP_X_BRANCH_ID=BRANCH,
    )

    assert response.status_code == 200
    assert response.json()['state'] == 'ACKNOWLEDGED'
    denied = client.get(
        '/api/sync/shift-close/ack',
        data=payload,
        HTTP_AUTHORIZATION='Branch branch-token',
        HTTP_X_BRANCH_ID='branch-b',
    )
    assert denied.status_code == 403


@pytest.mark.django_db
def test_nonempty_manifest_guards_legacy_reconciliation(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    _, shift, order = _closed_shift()
    Shift.objects.filter(pk=shift.pk).update(
        treasury_settlement_eligible=False,
    )
    OrderPayment.objects.filter(order=order).update(amount='126.00')
    manager = _user('close-manager@example.test', role='MANAGER')

    result, status = ShiftService.reconcile(
        shift.id,
        actual_cash='127.00',
        notes='must remain fail closed',
        reconciled_by_id=manager.id,
        confirmed={'CASH': '127.00'},
        actor=manager,
    )

    assert status == 422, result
    assert result['errors']['code'] == 'SETTLEMENT_SYNC_INCOMPLETE'
    assert not CashReconciliation.objects.filter(shift=shift).exists()
