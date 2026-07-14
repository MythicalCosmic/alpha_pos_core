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
