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
def test_shared_terminal_allows_each_cashier_to_keep_an_active_shift():
    from base.models import Shift
    from core.shifts.service import ShiftService

    first = _staff(branch='branch-a')
    same_till = _staff(branch='branch-a')
    other_till = _staff(branch='branch-a')

    result, status = ShiftService.start_shift(first.id, actor=first)
    assert status == 201, result
    assert result['data']['device_id'] == 'device-a'

    result, status = ShiftService.start_shift(same_till.id, actor=same_till)
    assert status == 201, result
    assert result['data']['device_id'] == 'device-a'

    with override_settings(DEVICE_ID='device-b'):
        result, status = ShiftService.start_shift(other_till.id, actor=other_till)
    assert status == 201, result
    assert Shift.objects.filter(
        status=Shift.Status.ACTIVE,
        end_time__isnull=True,
    ).count() == 3
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


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='',
)
def test_cashier_start_fails_closed_without_installation_identity():
    from base.models import Shift
    from base.services.shift_device import TERMINAL_DEVICE_ID_MISSING
    from core.shifts.service import ShiftService

    cashier = _staff(branch='branch-a')

    result, status = ShiftService.start_shift(cashier.id, actor=cashier)

    assert status == 400, result
    assert result['message'] == TERMINAL_DEVICE_ID_MISSING
    assert not Shift.objects.filter(user=cashier).exists()


@pytest.mark.parametrize('legacy_device', ['', 'device-b'])
@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_cashier_start_cannot_resume_blank_or_other_installation_shift(
    legacy_device,
):
    from base.models import Shift
    from base.services.shift_device import CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL
    from core.shifts.service import ShiftService

    cashier = _staff(branch='branch-a')
    legacy = Shift.objects.create(
        user=cashier,
        status=Shift.Status.ACTIVE,
        start_time=timezone.now(),
        branch_id='branch-a',
        device_id=legacy_device,
    )

    result, status = ShiftService.start_shift(cashier.id, actor=cashier)

    assert status == 400, result
    assert result['message'] == CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL
    legacy.refresh_from_db()
    assert legacy.status == Shift.Status.ACTIVE
    assert legacy.end_time is None


@pytest.mark.parametrize('legacy_device', ['', 'device-b'])
@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_cashier_can_close_legacy_or_other_installation_shift(
    legacy_device,
):
    from base.models import Shift
    from core.shifts.service import ShiftService

    cashier = _staff(branch='branch-a')
    legacy = Shift.objects.create(
        user=cashier,
        status=Shift.Status.ACTIVE,
        start_time=timezone.now() - timedelta(hours=1),
        branch_id='branch-a',
        device_id=legacy_device,
    )

    result, status = ShiftService.end_shift(
        legacy.id, cashier.id, notes='upgrade handoff', actor=cashier,
    )

    assert status == 200, result
    legacy.refresh_from_db()
    assert legacy.status == Shift.Status.ENDED
    assert legacy.end_time is not None


def test_database_allows_multiple_users_on_one_device_but_not_duplicate_user_shifts():
    from base.models import Shift

    first = _staff(branch='branch-a')
    second = _staff(branch='branch-a')
    now = timezone.now()

    Shift.objects.create(
        user=first, status=Shift.Status.ACTIVE, start_time=now,
        branch_id='branch-a', device_id='device-a',
    )
    Shift.objects.create(
        user=second, status=Shift.Status.ACTIVE,
        start_time=now + timedelta(seconds=1),
        branch_id='branch-a',
        device_id='device-a',
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Shift.objects.create(
                user=first,
                status=Shift.Status.ACTIVE,
                start_time=now + timedelta(seconds=2),
                branch_id='branch-a',
                device_id='device-a',
            )


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_ensure_active_shift_creates_once_then_resumes_same_row():
    from base.models import Shift
    from core.shifts.service import ShiftService

    cashier = _staff(branch='branch-a')

    created, created_status = ShiftService.ensure_active_shift(
        cashier.id,
        actor=cashier,
    )
    resumed, resumed_status = ShiftService.ensure_active_shift(
        cashier.id,
        actor=cashier,
    )

    assert created_status == 201, created
    assert created['data']['resumed'] is False
    assert resumed_status == 200, resumed
    assert resumed['data']['resumed'] is True
    assert resumed['data']['id'] == created['data']['id']
    assert Shift.objects.filter(
        user=cashier,
        status=Shift.Status.ACTIVE,
        end_time__isnull=True,
    ).count() == 1


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
