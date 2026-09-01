from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from base.models import CashRegister, Inkassa, Shift, User
from cashbox.models import CashboxExpense


pytestmark = pytest.mark.django_db


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch1')
def test_remote_drawer_expense_reversal_restores_applied_cash():
    actor = User.objects.create(
        first_name='Drawer', last_name='Tester', email='drawer@test.local',
        password='!', role='ADMIN', status='ACTIVE', branch_id='branch1',
    )
    shift = Shift.objects.create(
        user=actor,
        start_time=timezone.now(),
        status=Shift.Status.ACTIVE,
        branch_id='branch1',
    )
    register = CashRegister.objects.create(
        branch_id='branch1', current_balance='100',
    )
    original = CashboxExpense.objects.create(
        shift=shift,
        amount='30',
        register_command=True,
        comment=CashboxExpense.command_comment('remote expense'),
        created_by=actor,
        branch_id='branch1',
    )
    assert Inkassa._apply_pending_register_commands('branch1') is True
    register.refresh_from_db()
    assert register.current_balance == Decimal('70')
    assert register.remote_cash_out_applied_total == Decimal('30')

    CashboxExpense.objects.create(
        shift=shift,
        reversal_of=original,
        amount='-30',
        register_command=True,
        comment=CashboxExpense.command_comment('void'),
        created_by=actor,
        branch_id='branch1',
    )
    assert Inkassa.pending_register_amount(register) == Decimal('-30')
    assert Inkassa._apply_pending_register_commands('branch1') is True
    register.refresh_from_db()
    assert register.current_balance == Decimal('100')
    assert register.remote_cash_out_applied_total == Decimal('0')
    assert Inkassa.pending_register_amount(register) == Decimal('0')
