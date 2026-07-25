from decimal import Decimal
from contextlib import contextmanager
import uuid

import pytest
from django.db import transaction
from django.utils import timezone

from base.models import CashRegister, Shift, SyncQueueRecord, User
from base.services.sync.service import SyncService
from cashbox.models import ShiftPaymentTotal


pytestmark = pytest.mark.django_db


def _cloud_register(record_uuid, *, sync_version=3):
    return {
        'uuid': str(record_uuid),
        'sync_version': sync_version,
        'is_deleted': False,
        'branch_id': 'branch-a',
        'current_balance': '0.00',
        'remote_cash_out_applied_total': '0.00',
    }


def test_natural_key_pull_rebases_pending_local_payload_to_cloud_uuid(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = True

    local = CashRegister.objects.create(
        branch_id='branch-a',
        current_balance=Decimal('125000.00'),
        sync_version=5,
    )
    old_uuid = local.uuid
    assert SyncQueueRecord.objects.filter(
        model_name='cashregister',
        record_uuid=old_uuid,
    ).exists()
    cloud_uuid = uuid.uuid4()

    result = SyncService._apply_records(
        CashRegister,
        [_cloud_register(cloud_uuid)],
        source_branch='branch-a',
        authoritative_cloud=True,
    )

    local.refresh_from_db()
    assert result['updated'] == 1
    assert local.uuid == cloud_uuid
    # This field is locally owned and must still be published after UUID
    # convergence; the cloud's placeholder zero cannot consume it.
    assert local.current_balance == Decimal('125000.00')
    assert local.sync_version == 4
    assert local.synced_at is None
    assert not SyncQueueRecord.objects.filter(
        model_name='cashregister',
        record_uuid=old_uuid,
    ).exists()
    rebased = SyncQueueRecord.objects.get(
        model_name='cashregister',
        record_uuid=cloud_uuid,
    )
    assert rebased.payload['sync_version'] == 4
    assert Decimal(rebased.payload['current_balance']) == Decimal('125000.00')


def test_fk_natural_key_rekey_retires_old_queue_generation(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = True

    cashier = User.objects.create(
        first_name='FK',
        last_name='Cashier',
        email='fk-natural-key@test.local',
        password='hash',
        role='CASHIER',
        status='ACTIVE',
        branch_id='branch-a',
    )
    shift = Shift.objects.create(
        user=cashier,
        start_time=timezone.now(),
        branch_id='branch-a',
    )
    local = ShiftPaymentTotal.objects.create(
        shift=shift,
        method='CASH',
        expected_amount=Decimal('125000.00'),
        counted_amount=Decimal('125000.00'),
        confirmed_amount=Decimal('0.00'),
        difference=Decimal('0.00'),
        branch_id='branch-a',
    )
    old_uuid = local.uuid
    retired = SyncQueueRecord.objects.get(
        model_name='shiftpaymenttotal',
        record_uuid=old_uuid,
    )
    cloud_uuid = uuid.uuid4()
    cloud_record = local.to_sync_dict()
    cloud_record.update({
        'uuid': str(cloud_uuid),
        'sync_version': 3,
        'confirmed_amount': '125000.00',
    })

    result = SyncService._apply_records(
        ShiftPaymentTotal,
        [cloud_record],
        source_branch='branch-a',
        authoritative_cloud=True,
    )

    canonical = ShiftPaymentTotal.objects.get(pk=local.pk)
    assert result['updated'] == 1
    assert canonical.uuid == cloud_uuid
    assert canonical.expected_amount == Decimal('125000.00')
    assert canonical.confirmed_amount == Decimal('125000.00')
    assert not SyncQueueRecord.objects.filter(
        model_name='shiftpaymenttotal',
        record_uuid=old_uuid,
        generation=retired.generation,
    ).exists()
    queued = SyncQueueRecord.objects.get(
        model_name='shiftpaymenttotal',
        record_uuid=cloud_uuid,
    )
    assert queued.payload['uuid'] == str(cloud_uuid)
    assert queued.payload['shift_uuid'] == str(shift.uuid)


def test_rekey_snapshots_latest_generation_under_the_identity_lock(
    settings,
    monkeypatch,
):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = True

    local = CashRegister.objects.create(
        branch_id='branch-a',
        current_balance=Decimal('125000.00'),
    )
    old_uuid = local.uuid
    original_generation = SyncQueueRecord.objects.get(
        model_name='cashregister',
        record_uuid=old_uuid,
    ).generation
    cloud_uuid = uuid.uuid4()
    replacement_version = local.sync_version + 1
    replacement_payload = local.to_sync_dict()
    replacement_payload.update({
        'current_balance': '130000.00',
        'sync_version': replacement_version,
    })

    real_atomic = transaction.atomic
    rotated = False

    @contextmanager
    def rotate_before_record_work(*args, **kwargs):
        nonlocal rotated
        with real_atomic(*args, **kwargs):
            if not rotated:
                rotated = True
                CashRegister._base_manager.filter(pk=local.pk).update(
                    current_balance=Decimal('130000.00'),
                    sync_version=replacement_version,
                    synced_at=None,
                )
                SyncQueueRecord.objects.filter(
                    model_name='cashregister',
                    record_uuid=old_uuid,
                ).update(
                    payload=replacement_payload,
                    generation=uuid.uuid4(),
                    attempts=0,
                    last_error='',
                )
            yield

    monkeypatch.setattr(transaction, 'atomic', rotate_before_record_work)

    result = SyncService._apply_records(
        CashRegister,
        [_cloud_register(cloud_uuid)],
        source_branch='branch-a',
        authoritative_cloud=True,
    )

    canonical = CashRegister.objects.get(pk=local.pk)
    queued = list(SyncQueueRecord.objects.filter(model_name='cashregister'))
    assert rotated is True
    assert result['updated'] == 1
    assert canonical.uuid == cloud_uuid
    assert canonical.current_balance == Decimal('130000.00')
    assert len(queued) == 1
    assert queued[0].record_uuid == cloud_uuid
    assert queued[0].generation != original_generation
    assert queued[0].payload['current_balance'] == '130000.00'
    assert queued[0].last_error == ''


@pytest.mark.parametrize('partial_save', [False, True])
def test_preloaded_writer_cannot_restore_retired_uuid(
    settings,
    partial_save,
):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = True

    local = CashRegister.objects.create(
        branch_id='branch-a',
        current_balance=Decimal('125000.00'),
    )
    old_uuid = local.uuid
    stale_writer = CashRegister.objects.get(pk=local.pk)
    cloud_uuid = uuid.uuid4()

    result = SyncService._apply_records(
        CashRegister,
        [_cloud_register(cloud_uuid)],
        source_branch='branch-a',
        authoritative_cloud=True,
    )
    assert result['updated'] == 1

    stale_writer.current_balance = Decimal('140000.00')
    if partial_save:
        stale_writer.save(update_fields=['current_balance'])
    else:
        stale_writer.save()

    canonical = CashRegister.objects.get(pk=local.pk)
    queued = list(SyncQueueRecord.objects.filter(model_name='cashregister'))
    assert canonical.uuid == cloud_uuid
    assert canonical.current_balance == Decimal('140000.00')
    assert canonical.synced_at is None
    assert len(queued) == 1
    assert queued[0].record_uuid == cloud_uuid
    assert queued[0].payload['uuid'] == str(cloud_uuid)
    assert queued[0].payload['current_balance'] == '140000.00'
    assert not SyncQueueRecord.objects.filter(
        model_name='cashregister',
        record_uuid=old_uuid,
    ).exists()


def test_reconcile_reloads_canonical_uuid_after_stale_iteration(
    settings,
    monkeypatch,
):
    from base.services.sync import service as sync_service

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = True

    local = CashRegister.objects.create(
        branch_id='branch-a',
        current_balance=Decimal('125000.00'),
    )
    old_uuid = local.uuid
    stale_iterator_row = CashRegister.objects.get(pk=local.pk)
    cloud_uuid = uuid.uuid4()
    result = SyncService._apply_records(
        CashRegister,
        [_cloud_register(cloud_uuid)],
        source_branch='branch-a',
        authoritative_cloud=True,
    )
    assert result['updated'] == 1
    SyncQueueRecord.objects.filter(model_name='cashregister').delete()

    class StaleQuery:
        def filter(self, **kwargs):
            return self

        def order_by(self, *fields):
            return self

        def iterator(self):
            return iter([stale_iterator_row])

    monkeypatch.setattr(
        type(CashRegister.objects),
        'unsynced',
        lambda self: StaleQuery(),
    )
    monkeypatch.setattr(
        sync_service,
        'SYNC_ORDER',
        ['cashregister'],
    )
    monkeypatch.setattr(
        sync_service,
        'get_all_models',
        lambda: {'cashregister': CashRegister},
    )

    requeued = SyncService._reconcile_unsynced()

    queued = list(SyncQueueRecord.objects.filter(model_name='cashregister'))
    assert requeued == 1
    assert len(queued) == 1
    assert queued[0].record_uuid == cloud_uuid
    assert queued[0].payload['uuid'] == str(cloud_uuid)
    assert not SyncQueueRecord.objects.filter(
        model_name='cashregister',
        record_uuid=old_uuid,
    ).exists()


def test_preloaded_hard_delete_queues_canonical_tombstone(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_ENABLED = True

    local = CashRegister.objects.create(
        branch_id='branch-a',
        current_balance=Decimal('125000.00'),
    )
    old_uuid = local.uuid
    stale_writer = CashRegister.objects.get(pk=local.pk)
    cloud_uuid = uuid.uuid4()
    result = SyncService._apply_records(
        CashRegister,
        [_cloud_register(cloud_uuid)],
        source_branch='branch-a',
        authoritative_cloud=True,
    )
    assert result['updated'] == 1

    stale_writer.hard_delete()

    assert not CashRegister._base_manager.filter(pk=local.pk).exists()
    queued = list(SyncQueueRecord.objects.filter(model_name='cashregister'))
    assert len(queued) == 1
    assert queued[0].record_uuid == cloud_uuid
    assert queued[0].payload['uuid'] == str(cloud_uuid)
    assert queued[0].payload['is_deleted'] is True
    assert not SyncQueueRecord.objects.filter(
        model_name='cashregister',
        record_uuid=old_uuid,
    ).exists()
