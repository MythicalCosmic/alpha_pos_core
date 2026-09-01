from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _staff(*, role='CASHIER', branch='branch-a'):
    from base.models import User

    return User.objects.create(
        email=f'device-shift-{uuid4().hex}@test.local',
        first_name='Device',
        last_name='Cashier',
        password='!',
        role=role,
        status=User.UserStatus.ACTIVE,
        branch_id=branch,
    )


def _shift(user, *, device_id=''):
    from base.models import Shift

    return Shift.objects.create(
        user=user,
        start_time=timezone.now() - timedelta(hours=1),
        status=Shift.Status.ACTIVE,
        branch_id=user.branch_id,
        device_id=device_id,
    )


def _fund_cash(user, *, amount='100.00'):
    from base.models import CashRegister, Order, OrderPayment

    order = Order.objects.create(
        user=user,
        cashier=user,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        paid_at=timezone.now(),
        subtotal=amount,
        total_amount=amount,
        branch_id=user.branch_id,
    )
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.CASH,
        amount=amount,
        branch_id=user.branch_id,
    )
    CashRegister.objects.update_or_create(
        branch_id=user.branch_id,
        defaults={'current_balance': amount, 'is_deleted': False},
    )


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_cashier_settlement_requires_exact_installation_bound_shift():
    from base.services.order_refund import (
        SettlementInvariantError,
        lock_active_cashier_shift,
    )
    from base.services.shift_device import (
        CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL,
    )

    valid = _staff()
    valid_shift = _shift(valid, device_id='device-a')
    assert lock_active_cashier_shift(valid.id).pk == valid_shift.pk

    legacy = _staff()
    _shift(legacy, device_id='')
    with pytest.raises(
        SettlementInvariantError,
        match='^' + CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL.replace('.', r'\.'),
    ):
        lock_active_cashier_shift(legacy.id)

    foreign = _staff()
    _shift(foreign, device_id='device-b')
    with pytest.raises(
        SettlementInvariantError,
        match='^' + CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL.replace('.', r'\.'),
    ):
        lock_active_cashier_shift(foreign.id)


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='',
)
def test_cashier_settlement_fails_closed_without_installation_identity():
    from base.services.order_refund import (
        SettlementInvariantError,
        lock_active_cashier_shift,
    )
    from base.services.shift_device import TERMINAL_DEVICE_ID_MISSING

    cashier = _staff()
    _shift(cashier, device_id='')

    with pytest.raises(
        SettlementInvariantError,
        match='^' + TERMINAL_DEVICE_ID_MISSING.replace('.', r'\.'),
    ):
        lock_active_cashier_shift(cashier.id)


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_suspended_cashier_cannot_use_an_open_device_bound_shift():
    from base.models import User
    from base.services.order_refund import (
        SettlementInvariantError,
        lock_active_cashier_shift,
    )

    cashier = _staff()
    _shift(cashier, device_id='device-a')
    cashier.status = User.UserStatus.SUSPENDED
    cashier.save(update_fields=['status'])

    with pytest.raises(
        SettlementInvariantError,
        match='^Cashier not found or inactive\\.$',
    ):
        lock_active_cashier_shift(cashier.id)


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_manager_shift_intentionally_keeps_non_device_operational_semantics():
    from base.models import User
    from base.services.order_refund import lock_active_cashier_shift

    manager = _staff(role=User.RoleChoices.MANAGER)
    manager_shift = _shift(manager, device_id='')

    assert lock_active_cashier_shift(manager.id).pk == manager_shift.pk


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch-a', DEVICE_ID='device-a',
)
def test_cashier_drawer_expense_rejects_legacy_shift_but_manager_can_intervene():
    from base.models import User
    from base.services.shift_device import CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL
    from cashbox.models import CashboxExpense
    from cashbox.services.expense_service import CashboxExpenseService
    from hr.models import ExpenseCategory

    cashier = _staff()
    legacy = _shift(cashier, device_id='')
    _fund_cash(cashier)
    category = ExpenseCategory.objects.create(
        code='DRAWER_TEST',
        name='Drawer test',
        allowed_sources=['DRAWER'],
    )

    denied, denied_status = CashboxExpenseService.create(
        legacy.id,
        '5.00',
        category_id=category.id,
        comment='cashier attempt',
        actor=cashier,
    )
    assert denied_status == 403, denied
    assert denied['code'] == 'DRAWER_DEVICE_FORBIDDEN'
    assert denied['message'] == CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL
    assert not CashboxExpense.objects.exists()

    manager = _staff(role=User.RoleChoices.MANAGER)
    allowed, allowed_status = CashboxExpenseService.create(
        legacy.id,
        '5.00',
        category_id=category.id,
        comment='manager intervention',
        actor=manager,
    )
    assert allowed_status == 201, allowed
    assert CashboxExpense.objects.get().created_by_id == manager.id


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud', DEVICE_ID='')
def test_cloud_cashier_shift_keeps_explicit_non_device_admin_semantics():
    from base.services.order_refund import lock_active_cashier_shift

    cashier = _staff()
    shift = _shift(cashier, device_id='')

    assert lock_active_cashier_shift(
        cashier.id, branch_id='branch-a',
    ).pk == shift.pk
