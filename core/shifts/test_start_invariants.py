from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _staff(*, branch='branch-a', role='CASHIER'):
    from base.models import User

    return User.objects.create(
        email=f'shift-{uuid4().hex}@test.local',
        first_name='Shift',
        last_name='Tester',
        password='!',
        role=role,
        status=User.UserStatus.ACTIVE,
        branch_id=branch,
    )


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_start_shift_uses_cashier_branch_and_database_blocks_duplicate():
    from base.models import Shift
    from core.shifts.service import ShiftService

    cashier = _staff(branch='branch-a')
    result, status = ShiftService.start_shift(cashier.id, actor=cashier)
    assert status == 201, result
    shift = Shift.objects.get(pk=result['data']['id'])
    assert shift.branch_id == 'branch-a'

    result, status = ShiftService.start_shift(cashier.id, actor=cashier)
    assert status == 400, result
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Shift.objects.create(
                user=cashier,
                status=Shift.Status.ACTIVE,
                start_time=timezone.now() + timedelta(seconds=1),
                branch_id='branch-a',
            )
    assert Shift.objects.filter(
        user=cashier,
        status=Shift.Status.ACTIVE,
        end_time__isnull=True,
        is_deleted=False,
    ).count() == 1


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a')
def test_start_shift_rejects_blank_or_foreign_branch_ownership():
    from base.models import User
    from core.shifts.service import ShiftService

    foreign = _staff(branch='branch-b')
    result, status = ShiftService.start_shift(foreign.id, actor=foreign)
    assert status == 403, result

    blank = _staff(branch='branch-a')
    User.objects.filter(pk=blank.pk).update(branch_id='')
    blank.refresh_from_db()
    result, status = ShiftService.start_shift(blank.id, actor=blank)
    assert status == 403, result


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_cashier_cannot_start_another_users_shift():
    from core.shifts.service import ShiftService

    actor = _staff(branch='branch-a')
    target = _staff(branch='branch-a')
    result, status = ShiftService.start_shift(target.id, actor=actor)
    assert status == 403, result


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_cashier_device_slot_blocks_second_cashier_but_allows_other_tills():
    from base.models import Shift
    from core.shifts.service import ShiftService

    first = _staff(branch='branch-a')
    same_till = _staff(branch='branch-a')
    other_till = _staff(branch='branch-a')

    result, status = ShiftService.start_shift(first.id, actor=first)
    assert status == 201, result
    assert result['data']['device_id'] == 'device-a'

    result, status = ShiftService.start_shift(same_till.id, actor=same_till)
    assert status == 400, result
    assert result['message'] == 'This terminal already has an active cashier shift'

    with override_settings(DEVICE_ID='device-b'):
        result, status = ShiftService.start_shift(other_till.id, actor=other_till)
    assert status == 201, result
    assert Shift.objects.get(user=other_till).device_id == 'device-b'


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_non_cashier_shift_does_not_consume_cashier_device_slot():
    from base.models import Shift, User
    from core.shifts.service import ShiftService

    manager = _staff(role=User.RoleChoices.MANAGER, branch='branch-a')
    cashier = _staff(branch='branch-a')

    manager_result, manager_status = ShiftService.start_shift(
        manager.id, actor=manager,
    )
    cashier_result, cashier_status = ShiftService.start_shift(
        cashier.id, actor=cashier,
    )

    assert manager_status == 201, manager_result
    assert cashier_status == 201, cashier_result
    assert Shift.objects.get(user=manager).device_id == ''
    assert Shift.objects.get(user=cashier).device_id == 'device-a'


def test_database_device_constraint_and_blank_legacy_lane():
    from base.models import Shift

    first = _staff(branch='branch-a')
    second = _staff(branch='branch-a')
    now = timezone.now()

    # Migration 0054 deliberately leaves legacy ACTIVE rows blank. Multiple
    # unknown-device rows can finish normally because ownership cannot be
    # reconstructed safely after the fact.
    Shift.objects.create(
        user=first, status=Shift.Status.ACTIVE, start_time=now,
        branch_id='branch-a', device_id='',
    )
    Shift.objects.create(
        user=second, status=Shift.Status.ACTIVE,
        start_time=now + timedelta(seconds=1), branch_id='branch-a', device_id='',
    )

    Shift.objects.filter(user=first).update(device_id='device-a')
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Shift.objects.filter(user=second).update(device_id='device-a')


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_synced_shift_device_is_create_only_producer_evidence():
    from base.models import Shift
    from base.services.sync.receiver import CloudReceiver

    cashier = _staff(branch='branch-a')
    shift_uuid = uuid4()
    record = {
        'uuid': str(shift_uuid),
        'sync_version': 1,
        'is_deleted': False,
        'user_uuid': str(cashier.uuid),
        'start_time': timezone.now().isoformat(),
        'status': Shift.Status.ACTIVE,
        'device_id': 'device-a',
    }

    created = CloudReceiver.receive_batch('shift', 'branch-a', [record])
    assert created['created'] == 1, created
    shift = Shift.objects.get(uuid=shift_uuid)
    assert shift.device_id == 'device-a'

    record.update(sync_version=2, device_id='device-b', notes='later update')
    replayed = CloudReceiver.receive_batch('shift', 'branch-a', [record])
    assert replayed['updated'] == 1, replayed
    shift.refresh_from_db()
    assert shift.device_id == 'device-a'
    assert shift.notes == 'later update'
