from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _user(email=None, *, branch='main', role='CASHIER'):
    from base.models import User

    return User.objects.create(
        email=email or f'cash-{uuid4().hex}@test.local',
        first_name='Cash',
        last_name='Tester',
        password='!',
        role=role,
        status='ACTIVE',
        branch_id=branch,
    )


def _shift(user, *, status='ACTIVE', start=None, end=None, branch='main'):
    from base.models import Shift

    return Shift.objects.create(
        user=user,
        status=status,
        start_time=start or timezone.now() - timedelta(hours=1),
        end_time=end,
        branch_id=branch,
    )


def _paid_order(user, amount, *, paid_at=None, created_at=None, branch='main'):
    from base.models import Order, OrderPayment
    from base.services.inkassa_service import InkassaService

    paid_at = paid_at or timezone.now()
    order = Order.objects.create(
        user=user,
        cashier=user,
        status='COMPLETED',
        is_paid=True,
        payment_method='CASH',
        paid_at=paid_at,
        subtotal=amount,
        total_amount=amount,
        branch_id=branch,
    )
    if created_at is not None:
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()
    OrderPayment.objects.create(
        order=order, method='CASH', amount=amount, branch_id=branch,
    )
    InkassaService.add_to_register(Decimal(amount), branch)
    return order


class TestReconciliationInvariants:
    def _ended(self):
        from cashbox.models import ShiftPaymentTotal

        user = _user()
        shift = _shift(
            user,
            status='ENDED',
            end=timezone.now(),
        )
        ShiftPaymentTotal.objects.create(
            shift=shift,
            method='CASH',
            expected_amount='100.00',
            counted_amount='90.00',
            difference='-10.00',
        )
        ShiftPaymentTotal.objects.create(
            shift=shift,
            method='UZCARD',
            expected_amount='50.00',
            counted_amount='50.00',
            difference='0.00',
        )
        ShiftPaymentTotal.objects.create(
            shift=shift,
            method='PAYME',
            expected_amount='999.00',
            counted_amount='999.00',
            difference='0.00',
            is_deleted=True,
        )
        return user, shift

    def test_actual_cash_is_the_cash_confirmation_and_no_treasury_post(self):
        from base.models import CashReconciliation, TreasuryTransaction
        from cashbox.models import ShiftPaymentTotal
        from core.shifts.service import ShiftService

        user, shift = self._ended()
        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash='95',
            notes='manager count',
            reconciled_by_id=user.id,
            confirmed={'cash': '95.00', 'uzcard': '48'},
        )
        assert status == 201, result
        reconciliation = CashReconciliation.objects.get(shift=shift)
        assert reconciliation.expected_cash == Decimal('100.00')
        assert reconciliation.actual_cash == Decimal('95.00')
        assert reconciliation.difference == Decimal('-5.00')
        active = {
            row.method: row
            for row in ShiftPaymentTotal.objects.filter(
                shift=shift, is_deleted=False,
            )
        }
        assert active['CASH'].confirmed_amount == Decimal('95.00')
        assert active['UZCARD'].confirmed_amount == Decimal('48.00')
        assert ShiftPaymentTotal.objects.get(
            shift=shift, method='PAYME', is_deleted=True,
        ).confirmed_amount == Decimal('0.00')
        assert TreasuryTransaction.objects.count() == 0

    @pytest.mark.parametrize(
        ('actual', 'confirmed'),
        [
            ('-1', {'CASH': '0'}),
            ('100', {'CASH': '-1'}),
            ('100', {'CASH': '99'}),
            ('100', {'UZCARD': 'NaN'}),
            ('100', {'BOGUS': '1'}),
        ],
    )
    def test_invalid_or_contradictory_confirmation_is_atomic(
        self, actual, confirmed,
    ):
        from base.models import CashReconciliation
        from cashbox.models import ShiftPaymentTotal
        from core.shifts.service import ShiftService

        user, shift = self._ended()
        before = {
            row.pk: row.confirmed_amount
            for row in ShiftPaymentTotal.objects.filter(shift=shift)
        }
        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash=actual,
            notes='',
            reconciled_by_id=user.id,
            confirmed=confirmed,
        )
        assert status == 422, result
        assert not CashReconciliation.objects.filter(shift=shift).exists()
        shift.refresh_from_db()
        assert shift.status == 'ENDED'
        after = {
            row.pk: row.confirmed_amount
            for row in ShiftPaymentTotal.objects.filter(shift=shift)
        }
        assert after == before

    @override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
    def test_reconciliation_children_inherit_shift_branch_and_repair_legacy_rows(
        self,
    ):
        from base.models import CashReconciliation
        from cashbox.models import ShiftPaymentTotal
        from core.shifts.service import ShiftService

        user = _user(branch='branch-a')
        shift = _shift(
            user, status='ENDED', end=timezone.now(), branch='branch-a',
        )
        legacy = ShiftPaymentTotal.objects.create(
            shift=shift,
            method='UZCARD',
            expected_amount='0.00',
            counted_amount='0.00',
            difference='0.00',
            branch_id='cloud',
        )

        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash='0.00',
            notes='',
            reconciled_by_id=user.id,
            confirmed={'CASH': '0.00', 'UZCARD': '0.00'},
        )

        assert status == 201, result
        assert CashReconciliation.objects.get(shift=shift).branch_id == 'branch-a'
        assert ShiftPaymentTotal.objects.get(
            shift=shift, method='CASH', is_deleted=False,
        ).branch_id == 'branch-a'
        legacy.refresh_from_db()
        assert legacy.branch_id == 'branch-a'


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_end_shift_settlement_rows_inherit_shift_branch():
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    user = _user(branch='branch-a')
    shift = _shift(user, branch='branch-a')
    ShiftPaymentTotal.objects.create(
        shift=shift,
        method='CASH',
        expected_amount='999.00',
        counted_amount='999.00',
        difference='0.00',
        branch_id='cloud',
    )

    result, status = ShiftService.end_shift(
        shift.id, user.id, notes='', actor=user, counted={'CASH': '0.00'},
    )

    assert status == 200, result
    rows = ShiftPaymentTotal.objects.filter(shift=shift, is_deleted=False)
    assert rows.count() > 0
    assert set(rows.values_list('branch_id', flat=True)) == {'branch-a'}


def test_shift_totals_are_branch_scoped_and_handoff_is_half_open():
    from cashbox.services.drawer import expected_payment_totals
    from core.shifts.service import ShiftService

    user = _user(branch='branch-a')
    t0 = timezone.now() - timedelta(hours=3)
    handoff = t0 + timedelta(hours=1)
    t2 = handoff + timedelta(hours=1)
    first = _shift(
        user, status='ENDED', start=t0, end=handoff, branch='branch-a',
    )
    second = _shift(
        user, status='ENDED', start=handoff, end=t2, branch='branch-a',
    )

    # Exact boundary belongs to the later shift only.
    _paid_order(
        user, Decimal('100.00'), paid_at=handoff,
        created_at=handoff, branch='branch-a',
    )
    # Same global cashier and timestamp, but another branch: never leaks in.
    _paid_order(
        user, Decimal('900.00'), paid_at=handoff + timedelta(minutes=5),
        created_at=handoff + timedelta(minutes=5), branch='branch-b',
    )

    assert ShiftService._live_totals(first, handoff) == (
        0, Decimal('0.00'), Decimal('0.00'),
    )
    assert ShiftService._live_totals(second, t2) == (
        1, Decimal('100.00'), Decimal('100.00'),
    )
    assert expected_payment_totals(first)['CASH'] == Decimal('0.00')
    assert expected_payment_totals(second)['CASH'] == Decimal('100.00')

    extras = ShiftService._batch_list_extras([first, second], now=t2)
    assert Decimal(extras[first.id]['payment_mix'].get('cash', '0')) == Decimal('0')
    assert Decimal(extras[second.id]['payment_mix']['cash']) == Decimal('100.00')


def test_other_branch_open_order_does_not_block_shift_close():
    from base.models import Order
    from core.shifts.service import ShiftService

    user = _user(branch='branch-a')
    shift = _shift(user, branch='branch-a')
    Order.objects.create(
        user=user,
        cashier=user,
        branch_id='branch-b',
        status=Order.Status.OPEN,
        is_paid=False,
        subtotal='50.00',
        total_amount='50.00',
    )

    result, status = ShiftService.end_shift(
        shift.id, user.id, notes='', actor=user,
    )

    assert status == 200, result


def test_shift_detail_uses_paid_clock_and_net_active_lines():
    from base.models import Category, Order, OrderItem, Product
    from core.shifts.service import ShiftService

    user = _user()
    now = timezone.now()
    shift = _shift(
        user,
        status='ENDED',
        start=now - timedelta(hours=1),
        end=now + timedelta(minutes=1),
    )
    category = Category.objects.create(name='Food', slug=f'food-{uuid4().hex}')
    product = Product.objects.create(name='Meal', category=category, price='100')

    # Cart created before this shift, but money settled inside it.
    settled = _paid_order(
        user,
        Decimal('80.00'),
        paid_at=now,
        created_at=now - timedelta(hours=2),
    )
    settled.subtotal = Decimal('100.00')
    settled.discount_amount = Decimal('20.00')
    settled.save(update_fields=['subtotal', 'discount_amount'])
    OrderItem.objects.create(
        order=settled,
        product=product,
        quantity=1,
        price='100.00',
        original_price='100.00',
    )
    removed = OrderItem.objects.create(
        order=settled,
        product=product,
        quantity=5,
        price='100.00',
        original_price='100.00',
    )
    removed.delete()

    # Operational cart inside the shift, but unpaid: no realized units/revenue.
    unpaid = Order.objects.create(
        user=user,
        cashier=user,
        status='OPEN',
        is_paid=False,
        subtotal='500.00',
        total_amount='500.00',
    )
    OrderItem.objects.create(
        order=unpaid,
        product=product,
        quantity=5,
        price='100.00',
        original_price='100.00',
    )

    stats = ShiftService._shift_stats(shift, shift.end_time)
    assert Decimal(stats['payment_mix']['cash']) == Decimal('80.00')
    assert stats['units_sold'] == 1
    assert len(stats['category_stats']) == 1
    assert stats['category_stats'][0]['quantity'] == 1
    assert Decimal(stats['category_stats'][0]['revenue']) == Decimal('80.00')


def test_sale_and_refund_are_distinct_shift_settlement_events():
    from base.models import (
        Category, OrderItem, OrderRefund, Product,
    )
    from cashbox.models import ShiftPaymentTotal
    from cashbox.services.drawer import expected_payment_totals
    from core.shifts.service import ShiftService

    user = _user()
    now = timezone.now()
    sale_shift = _shift(
        user,
        status='ENDED',
        start=now - timedelta(hours=2),
        end=now - timedelta(hours=1),
    )
    refund_shift = _shift(
        user,
        status='ENDED',
        start=now - timedelta(minutes=30),
        end=now,
    )
    category = Category.objects.create(
        name='Refunded food', slug=f'refunded-{uuid4().hex}',
    )
    product = Product.objects.create(
        name='Refunded meal', category=category, price='80.00',
    )
    paid_at = sale_shift.start_time + timedelta(minutes=5)
    order = _paid_order(
        user,
        Decimal('80.00'),
        paid_at=paid_at,
        created_at=paid_at,
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        quantity=1,
        price='80.00',
        original_price='80.00',
    )
    # Operational cancellation does not rewrite or erase the original sale.
    order.status = 'CANCELED'
    order.save(update_fields=['status'])
    refund = OrderRefund.objects.create(
        order=order,
        shift=refund_shift,
        cashier=user,
        amount='80.00',
        cash_amount='80.00',
        drawer_cash_amount='80.00',
        card_amount='0.00',
        payme_amount='0.00',
        unknown_amount='0.00',
        refunded_at=refund_shift.start_time + timedelta(minutes=5),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=str(order.uuid),
        branch_id='main',
    )

    _, sale_revenue, sale_cash = ShiftService._live_totals(
        sale_shift, sale_shift.end_time,
    )
    _, refund_revenue, refund_cash = ShiftService._live_totals(
        refund_shift, refund_shift.end_time,
    )
    assert sale_revenue == Decimal('80.00')
    assert sale_cash == Decimal('80.00')
    assert refund_revenue == Decimal('-80.00')
    assert refund_cash == Decimal('-80.00')
    assert expected_payment_totals(sale_shift)['CASH'] == Decimal('80.00')
    assert expected_payment_totals(refund_shift)['CASH'] == Decimal('-80.00')

    sale_stats = ShiftService._shift_stats(sale_shift, sale_shift.end_time)
    refund_stats = ShiftService._shift_stats(refund_shift, refund_shift.end_time)
    assert Decimal(sale_stats['payment_mix']['cash']) == Decimal('80.00')
    assert sale_stats['units_sold'] == 1
    assert Decimal(sale_stats['category_stats'][0]['revenue']) == Decimal('80.00')
    assert Decimal(refund_stats['payment_mix']['cash']) == Decimal('-80.00')
    assert refund_stats['units_sold'] == -1
    assert Decimal(refund_stats['category_stats'][0]['revenue']) == Decimal('-80.00')

    # Signed expected movement is legitimate; the physical manager count and
    # confirmation remain non-negative.
    ShiftPaymentTotal.objects.create(
        shift=refund_shift,
        method='CASH',
        expected_amount='-80.00',
        counted_amount='0.00',
        difference='80.00',
    )
    result, status = ShiftService.reconcile(
        refund_shift.id,
        actual_cash='0.00',
        notes='opening float not modeled',
        reconciled_by_id=user.id,
        confirmed={'CASH': '0.00'},
    )
    assert status == 201, result
    assert result['data']['expected_cash'] == '-80.00'
    assert result['data']['difference'] == '80.00'


class TestCashboxExpenseInvariants:
    def test_expense_debits_register_and_cannot_overspend_or_post_after_close(self):
        from base.models import CashRegister
        from cashbox.services.drawer import drawer_cash
        from cashbox.services.expense_service import CashboxExpenseService

        user = _user()
        shift = _shift(user)
        _paid_order(user, Decimal('100.00'))

        result, status = CashboxExpenseService.create(
            shift.id, '60', comment='supplies', created_by=user,
        )
        assert status == 201, result
        register = CashRegister.objects.get(branch_id='main', is_deleted=False)
        assert register.current_balance == Decimal('40.00')
        assert drawer_cash(shift) == Decimal('40.00')

        result, status = CashboxExpenseService.create(
            shift.id, '50', comment='too much', created_by=user,
        )
        assert status == 422, result
        register.refresh_from_db()
        assert register.current_balance == Decimal('40.00')

        shift.status = 'ENDED'
        shift.end_time = timezone.now()
        shift.save(update_fields=['status', 'end_time'])
        result, status = CashboxExpenseService.create(
            shift.id, '1', comment='late', created_by=user,
        )
        assert status == 400, result

    def test_expense_reads_and_writes_are_shift_and_branch_scoped(self):
        from base.models import CashRegister
        from cashbox.models import CashboxExpense
        from cashbox.services.expense_service import CashboxExpenseService

        owner = _user(branch='main')
        shift = _shift(owner, branch='main')
        _paid_order(owner, Decimal('100.00'), branch='main')
        intruder = _user(branch='main')
        foreign_manager = _user(branch='other', role='MANAGER')
        own_manager = _user(branch='main', role='MANAGER')

        result, status = CashboxExpenseService.create(
            shift.id, '10.00', comment='owner expense', actor=owner,
        )
        assert status == 201, result

        for actor in (intruder, foreign_manager):
            result, status = CashboxExpenseService.create(
                shift.id, '1.00', comment='unauthorized', actor=actor,
            )
            assert status == 403, result
            result, status = CashboxExpenseService.list_for_shift(
                shift.id, actor=actor,
            )
            assert status == 403, result

        result, status = CashboxExpenseService.list_for_shift(
            shift.id, actor=owner,
        )
        assert status == 200, result
        assert len(result['data']) == 1
        result, status = CashboxExpenseService.create(
            shift.id, '5.00', comment='manager approved', actor=own_manager,
        )
        assert status == 201, result

        assert CashboxExpense.objects.filter(shift=shift).count() == 2
        assert CashRegister.objects.get(
            branch_id='main', is_deleted=False,
        ).current_balance == Decimal('85.00')

    @override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
    def test_cloud_expense_is_pending_command_not_register_overwrite(self):
        from base.models import CashRegister, Inkassa
        from cashbox.models import CashboxExpense
        from cashbox.services.expense_service import CashboxExpenseService

        user = _user(branch='branch-a')
        shift = _shift(user, branch='branch-a')
        _paid_order(user, Decimal('100.00'), branch='branch-a')
        register = CashRegister.objects.get(branch_id='branch-a')

        result, status = CashboxExpenseService.create(
            shift.id, '30', comment='remote approval', created_by=user,
        )
        assert status == 201, result
        register.refresh_from_db()
        assert register.current_balance == Decimal('100.00')
        assert Inkassa.pending_register_amount(register) == Decimal('30.00')
        expense = CashboxExpense.objects.get(pk=result['data']['id'])
        assert expense.register_command is True
        assert CashboxExpense.visible_comment(expense.comment) == 'remote approval'


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='branch-a')
def test_cash_inkassa_pull_applies_once_and_acknowledges_with_balance():
    from base.models import CashRegister, Inkassa

    register = CashRegister.objects.create(
        branch_id='branch-a', current_balance='100.00',
    )
    command_uuid = uuid4()
    payload = {
        'uuid': str(command_uuid),
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': 'branch-a',
        'amount': '30.00',
        'inkass_type': 'CASH',
        'balance_before': '100.00',
        'balance_after': '70.00',
        'register_command': True,
        'notes': Inkassa.command_notes('collect'),
    }
    Inkassa.from_sync_dict(payload, branch_id='branch-a')
    register.refresh_from_db()
    assert register.current_balance == Decimal('70.00')
    assert register.remote_cash_out_applied_total == Decimal('30.00')

    # Duplicate pull is harmless: cumulative command and acknowledgement match.
    Inkassa.from_sync_dict(payload, branch_id='branch-a')
    register.refresh_from_db()
    assert register.current_balance == Decimal('70.00')
    assert register.remote_cash_out_applied_total == Decimal('30.00')
