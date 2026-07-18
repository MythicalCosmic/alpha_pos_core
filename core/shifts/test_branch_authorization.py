from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _staff(*, branch, role):
    from base.models import User

    return User.objects.create(
        email=f'branch-shift-{uuid4().hex}@test.local',
        first_name='Branch',
        last_name='Guard',
        password='!',
        role=role,
        status=User.UserStatus.ACTIVE,
        branch_id=branch,
    )


def _shift(user, *, branch):
    from base.models import Shift

    return Shift.objects.create(
        user=user,
        status=Shift.Status.ACTIVE,
        start_time=timezone.now(),
        branch_id=branch,
    )


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_branch_manager_cannot_read_end_or_start_another_branch_shift():
    from core.shifts.service import ShiftService

    manager = _staff(branch='branch-a', role='MANAGER')
    foreign_cashier = _staff(branch='branch-b', role='CASHIER')
    foreign_shift = _shift(foreign_cashier, branch='branch-b')

    _result, status = ShiftService.get(foreign_shift.id, actor=manager)
    assert status == 403

    _result, status = ShiftService.end_shift(
        foreign_shift.id, manager.id, 'cross-branch attempt', actor=manager,
    )
    assert status == 403
    foreign_shift.refresh_from_db()
    assert foreign_shift.status == 'ACTIVE'

    _result, status = ShiftService.start_shift(
        foreign_cashier.id, actor=manager, branch_id='branch-b',
    )
    assert status == 403


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_shift_lists_and_active_rows_are_scoped_before_pagination():
    from core.shifts.service import ShiftService

    manager_a = _staff(branch='branch-a', role='MANAGER')
    cashier_a = _staff(branch='branch-a', role='CASHIER')
    other_a = _staff(branch='branch-a', role='CASHIER')
    cashier_b = _staff(branch='branch-b', role='CASHIER')
    global_admin = _staff(branch='cloud', role='ADMIN')
    own = _shift(cashier_a, branch='branch-a')
    same_branch = _shift(other_a, branch='branch-a')
    foreign = _shift(cashier_b, branch='branch-b')

    manager_result, manager_status = ShiftService.list(
        per_page=50, actor=manager_a,
    )
    assert manager_status == 200
    assert {row['id'] for row in manager_result['data']['shifts']} == {
        own.id, same_branch.id,
    }
    assert manager_result['data']['pagination']['total'] == 2

    cashier_result, cashier_status = ShiftService.get_active_shifts(
        actor=cashier_a,
    )
    assert cashier_status == 200
    assert {row['id'] for row in cashier_result['data']} == {own.id}

    global_result, global_status = ShiftService.get_active_shifts(
        actor=global_admin,
    )
    assert global_status == 200
    assert {row['id'] for row in global_result['data']} == {
        own.id, same_branch.id, foreign.id,
    }
