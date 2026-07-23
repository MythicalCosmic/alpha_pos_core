"""Tests for the per-shift drawer + cashbox expenses (P1/P4)."""
from django.test import override_settings
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


def _user(email='cashier@t.local', *, role='CASHIER'):
    from base.models import User
    return User.objects.create(
        first_name='Cash', last_name='Ier', email=email, password='x',
        role=role, status='ACTIVE')


def _shift(user):
    from base.models import Shift
    return Shift.objects.create(
        user=user,
        start_time=timezone.now(),
        status='ACTIVE',
        branch_id=user.branch_id,
        treasury_settlement_eligible=True,
    )


def _paid_cash_order(user, amount, method='CASH'):
    from base.models import Order, OrderPayment
    o = Order.objects.create(
        user=user, cashier=user, status='COMPLETED', is_paid=True,
        paid_at=timezone.now(), total_amount=amount, payment_method=method)
    OrderPayment.objects.create(order=o, method=method, amount=amount)
    if method == 'CASH':
        from base.services.inkassa_service import InkassaService
        InkassaService.add_to_register(Decimal(amount), o.branch_id)
    return o


class TestDrawer:
    def test_drawer_cash_from_payments(self):
        from cashbox.services.drawer import drawer_cash, expected_payment_totals
        u = _user(); s = _shift(u)
        _paid_cash_order(u, Decimal('100000'), 'CASH')
        _paid_cash_order(u, Decimal('40000'), 'UZCARD')
        assert drawer_cash(s) == Decimal('100000.00')
        totals = expected_payment_totals(s)
        assert totals['CASH'] == Decimal('100000.00')
        assert totals['UZCARD'] == Decimal('40000.00')

    def test_cash_expense_reduces_drawer(self):
        from cashbox.services.drawer import drawer_cash
        from cashbox.services.expense_service import CashboxExpenseService
        u = _user(); s = _shift(u)
        _paid_cash_order(u, Decimal('100000'), 'CASH')
        res, st = CashboxExpenseService.create(
            s.id, Decimal('30000'), comment='napkins', created_by=u)
        assert st == 201, res
        assert drawer_cash(s) == Decimal('70000.00')


def _authenticated_client(user):
    import secrets
    from datetime import timedelta

    from django.test import Client
    from base.models import Session
    from base.repositories import SessionRepository

    token = secrets.token_hex(32)
    Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        user_agent='cashbox-contract-test',
        payload=SessionRepository.hash_token(token),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    client = Client(HTTP_USER_AGENT='cashbox-contract-test')
    client.cookies['session_key'] = token
    return client


def test_cashier_can_list_cashbox_categories_but_cannot_create_them():
    import json
    from cashbox.models import CashboxExpenseCategory

    CashboxExpenseCategory.objects.create(name='Supplies', sort_order=1)
    cashier = _authenticated_client(_user(role='CASHIER'))

    listed = cashier.get('/api/admins/cashbox/categories/')
    denied = cashier.post(
        '/api/admins/cashbox/categories/',
        data=json.dumps({'name': 'Unauthorized'}),
        content_type='application/json',
    )

    assert listed.status_code == 200
    assert any(row['name'] == 'Supplies' for row in listed.json()['data'])
    assert denied.status_code == 403
    assert not CashboxExpenseCategory.objects.filter(name='Unauthorized').exists()


def test_admin_can_create_cashbox_category():
    import json

    admin = _authenticated_client(_user(role='ADMIN'))
    response = admin.post(
        '/api/admins/cashbox/categories/',
        data=json.dumps({'name': 'Maintenance', 'sort_order': 2}),
        content_type='application/json',
    )

    assert response.status_code == 201, response.content


def test_manager_can_create_cashbox_category():
    import json

    manager = _authenticated_client(_user(role='MANAGER'))
    response = manager.post(
        '/api/admins/cashbox/categories/',
        data=json.dumps({'name': 'Manager maintenance', 'sort_order': 3}),
        content_type='application/json',
    )

    assert response.status_code == 201, response.content


class TestCashboxExpenseRecipients:
    def test_supplier_recipient_reduces_supplier_balance(self):
        from stock.models import Supplier
        from cashbox.services.expense_service import CashboxExpenseService
        u = _user(); s = _shift(u)
        _paid_cash_order(u, Decimal('20000'), 'CASH')
        sup = Supplier.objects.create(name='Veg Co', current_balance=Decimal('50000'))
        res, st = CashboxExpenseService.create(
            s.id, Decimal('20000'), recipient_supplier_id=sup.id, created_by=u)
        assert st == 201, res
        sup.refresh_from_db()
        assert sup.current_balance == Decimal('30000.00')

    def test_two_recipients_rejected(self):
        from base.models import User
        from stock.models import Supplier
        from cashbox.services.expense_service import CashboxExpenseService
        u = _user(); s = _shift(u)
        other = User.objects.create(first_name='A', last_name='B',
                                    email='a@t.local', password='x', role='CASHIER')
        sup = Supplier.objects.create(name='Veg Co')
        res, st = CashboxExpenseService.create(
            s.id, Decimal('1000'), recipient_user_id=other.id,
            recipient_supplier_id=sup.id, created_by=u)
        assert st >= 400


class TestShiftSettlement:
    def test_close_and_confirm_freezes_and_posts_all_tenders_to_safe(self):
        from base.models import TreasuryAccount, TreasuryTransaction
        from core.shifts.service import ShiftService
        from cashbox.models import ShiftPaymentTotal
        u = _user(role='MANAGER'); s = _shift(u)
        _paid_cash_order(u, Decimal('100000'), 'CASH')
        _paid_cash_order(u, Decimal('40000'), 'UZCARD')
        res, st = ShiftService.end_shift(
            s.id, u.id, '', actor=u,
            counted={'CASH': '100000', 'UZCARD': '40000'})
        assert st == 200, res
        s.refresh_from_db()
        assert s.status == 'ENDED'
        cash_spt = ShiftPaymentTotal.objects.get(shift=s, method='CASH')
        assert cash_spt.expected_amount == Decimal('100000.00')
        assert cash_spt.counted_amount == Decimal('100000.00')
        assert cash_spt.difference == Decimal('0.00')

        res, st = ShiftService.reconcile(
            s.id, actual_cash='100000', notes='', reconciled_by_id=u.id,
            confirmed={'CASH': '100000', 'UZCARD': '40000'})
        assert st == 201, res
        s.refresh_from_db()
        assert s.status == 'COMPLETED'
        assert TreasuryAccount.objects.get(kind='SAFE').balance == Decimal('140000.00')
        assert not TreasuryAccount.objects.filter(kind='BANK').exists()
        assert TreasuryTransaction.objects.filter(
            type='SHIFT_DEPOSIT', reference_type='ShiftSettlement',
        ).count() == 2
        assert res['data']['treasury_posting']['total'] == '140000.00'


class TestShiftPaymentTotalSync:
    """ShiftPaymentTotal is immutable close evidence.

    A natural-key collision under another UUID is not permission to rewrite the
    first event. ACK protocol v2 rejects the conflicting identity so the sender
    cannot delete evidence it failed to store. Peer tombstones are rejected for
    the same reason: append-only close evidence cannot be deleted through sync.
    """

    @override_settings(DEPLOYMENT_MODE='cloud')
    def test_collision_preserves_first_immutable_event_without_duplicate(self):
        import uuid as _uuid
        from cashbox.models import ShiftPaymentTotal
        from base.services.sync.receiver import CloudReceiver
        u = _user(); s = _shift(u)
        ShiftPaymentTotal.objects.create(
            shift=s,
            method='CASH',
            expected_amount=Decimal('100'),
            sync_version=1,
            branch_id=s.branch_id,
        )
        incoming = str(_uuid.uuid4())
        result = CloudReceiver.receive_batch('shiftpaymenttotal', s.branch_id, [{
            'uuid': incoming, 'sync_version': 5, 'is_deleted': False,
            'shift_uuid': str(s.uuid), 'method': 'CASH',
            'expected_amount': '250', 'counted_amount': '250',
            'confirmed_amount': '0', 'difference': '0',
        }])
        assert result['errors'], result
        assert result['skipped'] == 1
        assert result['acknowledged_uuids'] == []
        assert result['rejected_uuids'] == [incoming]
        assert result['record_results'][0]['reason_code'] == (
            'APPEND_ONLY_IDENTITY_CONFLICT'
        )
        assert ShiftPaymentTotal.objects.filter(shift=s, method='CASH').count() == 1
        row = ShiftPaymentTotal.objects.get(shift=s, method='CASH')
        assert str(row.uuid) != incoming
        assert row.expected_amount == Decimal('100.00')

    def test_tombstone_with_missing_shift_is_skipped(self):
        import uuid as _uuid
        from base.services.sync.receiver import CloudReceiver
        incoming = str(_uuid.uuid4())
        result = CloudReceiver.receive_batch('shiftpaymenttotal', 'branch1', [{
            'uuid': incoming, 'sync_version': 1, 'is_deleted': True,
            'shift_uuid': str(_uuid.uuid4()), 'method': 'CASH', 'expected_amount': '0',
        }])
        assert result['errors']
        assert result['skipped'] == 1
        assert result['acknowledged_uuids'] == []
        assert result['rejected_uuids'] == [incoming]
        assert result['failed_uuids'] == [incoming]
        assert result['record_results'][0]['reason_code'] == (
            'APPEND_ONLY_DELETE'
        )
