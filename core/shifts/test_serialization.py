import logging
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_unreconciled_shift_list_is_normal_and_does_not_log_exception(caplog):
    from base.models import Shift, User
    from core.shifts.service import ShiftService

    user = User.objects.create(
        email=f'unreconciled-{uuid4().hex}@test.local',
        first_name='Unreconciled',
        last_name='Cashier',
        password='!',
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
        branch_id='main',
    )
    now = timezone.now()
    for offset in (2, 1):
        Shift.objects.create(
            user=user,
            status=Shift.Status.ENDED,
            start_time=now - timedelta(hours=offset + 1),
            end_time=now - timedelta(hours=offset),
            branch_id='main',
        )

    with caplog.at_level(logging.ERROR):
        result, status = ShiftService.list(user_id=user.id)

    assert status == 200, result
    assert len(result['data']['shifts']) == 2
    assert all(row['reconciliation'] is None for row in result['data']['shifts'])
    assert 'failed to serialize shift reconciliation' not in caplog.text
