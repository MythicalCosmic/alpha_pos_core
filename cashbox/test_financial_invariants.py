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


def _shift(user, *, status='ACTIVE', start=None, end=None, branch='main',
           treasury_eligible=True):
    from base.models import Shift

    return Shift.objects.create(
        user=user,
        status=status,
        start_time=start or timezone.now() - timedelta(hours=1),
        end_time=end,
        branch_id=branch,
        treasury_settlement_eligible=treasury_eligible,
    )


def _paid_order(user, amount, *, paid_at=None, created_at=None, branch='main',
                method='CASH'):
    from base.models import Order, OrderPayment
    from base.services.inkassa_service import InkassaService

    paid_at = paid_at or timezone.now()
    order = Order.objects.create(
        user=user,
        cashier=user,
        status='COMPLETED',
        is_paid=True,
        payment_method=method,
        paid_at=paid_at,
        subtotal=amount,
        total_amount=amount,
        branch_id=branch,
    )
    if created_at is not None:
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()
    OrderPayment.objects.create(
        order=order, method=method, amount=amount, branch_id=branch,
    )
    if method == 'CASH':
        InkassaService.add_to_register(Decimal(amount), branch)
    return order


def _freeze_settlement(shift, counted=None):
    from cashbox.models import ShiftPaymentTotal
    from cashbox.services.drawer import expected_payment_totals
    from core.shifts.service import _build_settlement_manifest

    counted = counted or {}
    rows = []
    for method, expected in expected_payment_totals(shift).items():
        count = Decimal(str(counted.get(method, max(expected, Decimal('0.00')))))
        row = ShiftPaymentTotal.objects.create(
            shift=shift,
            method=method,
            expected_amount=expected,
            counted_amount=count,
            difference=count - expected,
            branch_id=shift.branch_id,
        )
        rows.append(row)
    shift.settlement_manifest = _build_settlement_manifest(shift, rows)
    shift.save(update_fields=['settlement_manifest'])
    return rows


def _tender_evidence(shift, user, amounts):
    from base.models import OrderRefund

    paid_at = shift.start_time + timedelta(minutes=5)
    for method, raw in amounts.items():
        amount = Decimal(str(raw))
        if amount > 0:
            _paid_order(
                user, amount, paid_at=paid_at, created_at=paid_at,
                branch=shift.branch_id, method=method,
            )
            continue
        if amount >= 0:
            continue
        original = _paid_order(
            user,
            -amount,
            paid_at=shift.start_time - timedelta(hours=1),
            created_at=shift.start_time - timedelta(hours=1),
            branch=shift.branch_id,
            method=method,
        )
        kwargs = {
            'cash_amount': '0.00',
            'drawer_cash_amount': '0.00',
            'card_amount': '0.00',
            'payme_amount': '0.00',
            'unknown_amount': '0.00',
            'card_detail': {},
        }
        if method == 'CASH':
            kwargs['cash_amount'] = -amount
            kwargs['drawer_cash_amount'] = -amount
        elif method == 'PAYME':
            kwargs['payme_amount'] = -amount
        elif method in ('UZCARD', 'HUMO', 'CARD'):
            kwargs['card_amount'] = -amount
            kwargs['card_detail'] = {method: str(-amount)}
        else:
            kwargs['unknown_amount'] = -amount
        OrderRefund.objects.create(
            order=original,
            shift=shift,
            cashier=user,
            amount=-amount,
            refunded_at=paid_at,
            source=OrderRefund.Source.ORDER_CANCEL,
            source_id=f'test-{shift.id}-{method}-{uuid4().hex}',
            branch_id=shift.branch_id,
            **kwargs,
        )


class TestReconciliationInvariants:
    def _ended(self, amounts=None):
        user = _user(role='MANAGER')
        shift = _shift(
            user,
            status='ENDED',
            end=timezone.now() + timedelta(minutes=1),
        )
        amounts = amounts or {'CASH': '100.00', 'UZCARD': '50.00'}
        _tender_evidence(shift, user, amounts)
        _freeze_settlement(
            shift,
            counted={
                method: ('90.00' if method == 'CASH' else max(Decimal(str(value)), 0))
                for method, value in amounts.items()
            },
        )
        from cashbox.models import ShiftPaymentTotal
        ShiftPaymentTotal.objects.create(
            shift=shift,
            method='PAYME',
            expected_amount='999.00',
            counted_amount='999.00',
            difference='0.00',
            is_deleted=True,
        )
        return user, shift

    def test_actual_cash_and_every_confirmed_tender_post_to_safe_once(self):
        from base.models import (
            CashReconciliation, TreasuryAccount, TreasuryTransaction,
        )
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
        posting = result['data']['treasury_posting']
        assert posting == {
            'status': 'posted',
            'account': 'SAFE',
            'total': '143.00',
            'tenders': [
                {'method': 'CASH', 'amount': '95.00'},
                {'method': 'UZCARD', 'amount': '48.00'},
            ],
            'entry_ids': posting['entry_ids'],
        }
        assert len(posting['entry_ids']) == 2
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('143.00')
        assert not TreasuryAccount.objects.filter(kind='BANK').exists()
        assert set(TreasuryTransaction.objects.values_list('category', flat=True)) == {
            'CASH', 'UZCARD',
        }

        # Same request is an idempotent read/recovery path. It returns the same
        # authoritative entry ids and cannot credit SAFE twice.
        retry, retry_status = ShiftService.reconcile(
            shift.id,
            actual_cash='95.00',
            notes='safe retry',
            reconciled_by_id=user.id,
            confirmed={'CASH': '95.00', 'UZCARD': '48.00'},
        )
        assert retry_status == 200, retry
        assert retry['data']['treasury_posting']['entry_ids'] == posting['entry_ids']
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('143.00')
        assert TreasuryTransaction.objects.count() == 2

        conflict, conflict_status = ShiftService.reconcile(
            shift.id,
            actual_cash='95.00',
            notes='',
            reconciled_by_id=user.id,
            confirmed={'CASH': '95.00', 'UZCARD': '49.00'},
        )
        assert conflict_status == 422, conflict
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('143.00')
        assert TreasuryTransaction.objects.count() == 2

    def test_unpaid_ready_order_from_old_close_blocks_cloud_reconciliation(self):
        """The hub protects shifts closed by clients with the old OPEN-only guard."""
        from base.models import CashReconciliation, Order, TreasuryTransaction
        from core.shifts.service import ShiftService

        user, shift = self._ended()
        Order.objects.create(
            user=user,
            cashier=user,
            branch_id=shift.branch_id,
            status=Order.Status.READY,
            is_paid=False,
            subtotal='127000.00',
            total_amount='127000.00',
        )

        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash='100.00',
            notes='must not post an incomplete close',
            reconciled_by_id=user.id,
            confirmed={'CASH': '100.00', 'UZCARD': '50.00'},
        )

        assert status == 422, result
        assert result['errors']['code'] == 'SETTLEMENT_SYNC_INCOMPLETE'
        assert 'unpaid' in result['errors']['settlement'].lower()
        assert not CashReconciliation.objects.filter(shift=shift).exists()
        assert not TreasuryTransaction.objects.filter(
            reference_type='ShiftSettlement',
            reference_id=shift.id,
        ).exists()

    def test_mixed_all_tenders_post_to_safe_and_zero_is_ledger_noop(self):
        from base.models import TreasuryAccount, TreasuryTransaction
        from core.shifts.service import ShiftService

        amounts = {
            'CASH': '100.00',
            'HUMO': '20.00',
            'UZCARD': '30.00',
            'CARD': '40.00',
            'PAYME': '50.00',
        }
        user, shift = self._ended(amounts)

        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash='100.00',
            notes='all tenders',
            reconciled_by_id=user.id,
            confirmed=amounts,
        )
        assert status == 201, result
        posting = result['data']['treasury_posting']
        assert posting['account'] == 'SAFE'
        assert posting['total'] == '240.00'
        assert len(posting['entry_ids']) == 5
        assert {row['method'] for row in posting['tenders']} == set(amounts)
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('240.00')
        assert not TreasuryAccount.objects.filter(kind='BANK').exists()
        assert TreasuryTransaction.objects.count() == 5

    def test_net_negative_mixed_tenders_credit_and_reverse_safe_atomically(self):
        from base.models import TreasuryAccount, TreasuryTransaction
        from core.shifts.service import ShiftService

        user, shift = self._ended({'CASH': '20.00', 'PAYME': '-30.00'})

        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash='20.00',
            notes='cash sale plus provider refund',
            reconciled_by_id=user.id,
            confirmed={'CASH': '20.00', 'PAYME': '0.00'},
        )
        assert status == 201, result
        posting = result['data']['treasury_posting']
        assert posting['total'] == '-10.00'
        assert posting['tenders'] == [
            {'method': 'CASH', 'amount': '20.00'},
            {'method': 'PAYME', 'amount': '-30.00'},
        ]
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('-10.00')
        assert not TreasuryAccount.objects.filter(kind='BANK').exists()
        assert {
            row.category: row.delta
            for row in TreasuryTransaction.objects.filter(
                type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
            )
        } == {'CASH': Decimal('20.00'), 'PAYME': Decimal('-30.00')}

    def test_legacy_reconciliation_retry_never_backfills_safe(self):
        from base.models import (
            CashReconciliation, TreasuryAccount, TreasuryTransaction,
        )
        from cashbox.models import ShiftPaymentTotal
        from core.shifts.service import ShiftService

        user, shift = self._ended()
        ShiftPaymentTotal.objects.filter(
            shift=shift, method='CASH', is_deleted=False,
        ).update(confirmed_amount='95.00')
        ShiftPaymentTotal.objects.filter(
            shift=shift, method='UZCARD', is_deleted=False,
        ).update(confirmed_amount='48.00')
        CashReconciliation.objects.create(
            shift=shift,
            expected_cash='100.00',
            actual_cash='95.00',
            difference='-5.00',
            reconciled_by=user,
            # Null is the migration default for pre-rollout reconciliations.
            treasury_posted_at=None,
        )
        shift.status = 'COMPLETED'
        shift.save(update_fields=['status'])
        safe = TreasuryAccount.objects.create(kind='SAFE', balance='100.00')
        TreasuryTransaction.objects.create(
            account=safe,
            type=TreasuryTransaction.Type.INKASSA,
            delta='100.00',
            balance_before='0.00',
            balance_after='100.00',
            description='Historical pre-rollout Inkassa credit',
        )

        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash='95.00',
            notes='',
            reconciled_by_id=user.id,
            confirmed={'CASH': '95.00', 'UZCARD': '48.00'},
        )
        assert status == 200, result
        posting = result['data']['treasury_posting']
        assert posting['status'] == 'not_posted'
        assert posting['reason'] == 'LEGACY_RECONCILIATION_NOT_REPOSTED'
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('100.00')
        assert not TreasuryTransaction.objects.filter(
            type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
            reference_type='ShiftSettlement',
        ).exists()

    def test_pre_rollout_ended_unreconciled_shift_cannot_double_credit(self):
        from base.models import TreasuryAccount, TreasuryTransaction
        from cashbox.models import ShiftPaymentTotal
        from core.shifts.service import ShiftService

        user = _user(role='MANAGER')
        shift = _shift(
            user,
            status='ENDED',
            end=timezone.now(),
            treasury_eligible=False,
        )
        for method, amount in {'CASH': '100.00', 'PAYME': '50.00'}.items():
            ShiftPaymentTotal.objects.create(
                shift=shift,
                method=method,
                expected_amount=amount,
                counted_amount=amount,
                difference='0.00',
            )

        # Represents proceeds already recognized by the legacy Inkassa
        # lifecycle before 0048. There was no shift reference in that ledger.
        safe = TreasuryAccount.objects.create(kind='SAFE', balance='150.00')
        TreasuryTransaction.objects.create(
            account=safe,
            type=TreasuryTransaction.Type.INKASSA,
            delta='150.00',
            balance_before='0.00',
            balance_after='150.00',
            description='Historical mixed collection',
        )

        result, status = ShiftService.reconcile(
            shift.id,
            actual_cash='100.00',
            notes='late manager audit',
            reconciled_by_id=user.id,
            confirmed={'CASH': '100.00', 'PAYME': '50.00'},
        )
        assert status == 201, result
        assert result['data']['treasury_posted_at'] is None
        assert result['data']['treasury_posting']['status'] == 'not_posted'
        assert (
            result['data']['treasury_posting']['reason']
            == 'LEGACY_SHIFT_NOT_ELIGIBLE'
        )
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('150.00')
        assert TreasuryTransaction.objects.count() == 1
        assert not TreasuryTransaction.objects.filter(
            type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
        ).exists()

    def test_pre_upgrade_offline_shift_sync_defaults_fail_closed(self):
        from base.models import Shift

        user = _user(branch='branch-a')
        incoming_uuid = uuid4()
        payload = {
            'uuid': str(incoming_uuid),
            'sync_version': 3,
            'is_deleted': False,
            'branch_id': 'branch-a',
            'user_uuid': str(user.uuid),
            'shift_template_uuid': None,
            'start_time': (timezone.now() - timedelta(hours=2)).isoformat(),
            'end_time': (timezone.now() - timedelta(hours=1)).isoformat(),
            'status': 'ENDED',
            'total_orders': 2,
            'total_revenue': '150.00',
            'cash_collected': '100.00',
            'notes': 'old desktop payload has no eligibility field',
            # Deliberately no treasury_settlement_eligible key.
        }

        shift, action = Shift.from_sync_dict(payload, branch_id='branch-a')

        assert action == 'created'
        assert str(shift.uuid) == str(incoming_uuid)
        assert shift.status == 'ENDED'
        assert shift.treasury_settlement_eligible is False

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

        user = _user(branch='branch-a', role='MANAGER')
        shift = _shift(
            user, status='ENDED', end=timezone.now(), branch='branch-a',
            treasury_eligible=False,
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


@override_settings(DEPLOYMENT_MODE='local', BRANCH_ID='main')
def test_updated_start_shift_explicitly_opts_into_safe_settlement():
    from base.models import Shift
    from core.shifts.service import ShiftService

    user = _user(branch='main')
    result, status = ShiftService.start_shift(user.id, actor=user)

    assert status == 201, result
    shift = Shift.objects.get(pk=result['data']['id'])
    assert shift.status == 'ACTIVE'
    assert shift.treasury_settlement_eligible is True


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

    user = _user(role='MANAGER')
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
        Category, OrderItem, OrderRefund, Product, TreasuryAccount,
        TreasuryTransaction,
    )
    from cashbox.services.drawer import expected_payment_totals
    from core.shifts.service import ShiftService

    user = _user(role='MANAGER')
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
    OrderRefund.objects.create(
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
    _freeze_settlement(refund_shift, counted={'CASH': '0.00'})
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
    posting = result['data']['treasury_posting']
    assert posting['status'] == 'posted'
    assert posting['account'] == 'SAFE'
    assert posting['total'] == '-80.00'
    assert posting['tenders'] == [{'method': 'CASH', 'amount': '-80.00'}]
    assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('-80.00')
    entry = TreasuryTransaction.objects.get(
        type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
        reference_type='ShiftSettlement',
        reference_id=refund_shift.id,
        category='CASH',
    )
    assert entry.delta == Decimal('-80.00')
    assert 'refund reversal' in entry.description

    retry, retry_status = ShiftService.reconcile(
        refund_shift.id,
        actual_cash='0.00',
        notes='',
        reconciled_by_id=user.id,
        confirmed={'CASH': '0.00'},
    )
    assert retry_status == 200, retry
    assert retry['data']['treasury_posting']['entry_ids'] == posting['entry_ids']
    assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('-80.00')
    assert TreasuryTransaction.objects.count() == 1


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
