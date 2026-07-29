from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _cashier(email):
    from base.models import User

    return User.objects.create(
        email=email,
        first_name='Tender',
        last_name='Integrity',
        password='!',
        role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE,
        branch_id='branch1',
    )


def _shift(cashier, *, status='ENDED'):
    from base.models import Shift

    now = timezone.now()
    return Shift.objects.create(
        user=cashier,
        branch_id='branch1',
        start_time=now - timedelta(hours=1),
        end_time=(now + timedelta(seconds=1) if status != 'ACTIVE' else None),
        status=status,
        total_orders=1,
        total_revenue='100.00',
        cash_collected='0.00',
    )


def _paid_order(
    cashier,
    shift,
    *,
    total,
    method,
    paid_at=None,
    payment_action_id=None,
):
    from base.models import Order

    return Order.objects.create(
        user=cashier,
        cashier=cashier,
        branch_id=shift.branch_id,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=method,
        payment_action_id=payment_action_id,
        paid_at=paid_at or timezone.now(),
        subtotal=total,
        total_amount=total,
    )


def _freeze(shift, amounts):
    from cashbox.models import PAYMENT_METHODS, ShiftPaymentTotal

    for method in PAYMENT_METHODS:
        amount = Decimal(str(amounts.get(method, '0.00')))
        ShiftPaymentTotal.objects.create(
            shift=shift,
            branch_id=shift.branch_id,
            method=method,
            expected_amount=amount,
            counted_amount=amount,
            difference='0.00',
        )


def test_five_zero_rows_do_not_hide_mixed_order_with_missing_children():
    """Method presence alone cannot bless an unexplained paid order."""
    from core.shifts.service import ShiftService

    cashier = _cashier('missing-mixed-children@test.local')
    shift = _shift(cashier)
    _paid_order(cashier, shift, total='100.00', method='MIXED')
    _freeze(shift, {})

    response, status = ShiftService.get(shift.id, actor=cashier)

    assert status == 200, response
    row = response['data']
    assert row['expected_by_tender'] == {
        'CASH': '0.00',
        'UZCARD': '0.00',
        'HUMO': '0.00',
        'CARD': '0.00',
        'PAYME': '0.00',
        'UNKNOWN': '100.00',
    }
    assert row['total_expected_to_receive'] == '100.00'
    assert row['tender_totals_source'] == 'DERIVED_INCOMPLETE_FROZEN'
    assert row['frozen_tender_evidence_complete'] is False
    assert row['tender_attribution_complete'] is False
    assert row['unattributed_expected_amount'] == '100.00'
    assert row['frozen_tender_evidence_issues'] == [
        'UNATTRIBUTED_TENDER_EVIDENCE',
    ]
    assert row['frozen_tender_discrepancies'] == {
        'UNKNOWN': {
            'frozen': None,
            'derived': '100.00',
            'derived_minus_frozen': '100.00',
        },
    }


@pytest.mark.parametrize('method', ['CASH', 'HUMO'])
def test_action_identified_single_tender_without_children_is_unknown(method):
    """New checkout headers may never use the legacy method-only fallback."""
    from core.shifts.service import ShiftService

    cashier = _cashier(f'missing-{method.lower()}-child@test.local')
    shift = _shift(cashier)
    _paid_order(
        cashier,
        shift,
        total='100.00',
        method=method,
        payment_action_id=uuid4(),
    )
    _freeze(shift, {method: '100.00'})

    row = ShiftService._batch_list_extras([shift])[shift.id]

    assert row['expected_by_tender'] == {
        'CASH': '0.00',
        'UZCARD': '0.00',
        'HUMO': '0.00',
        'CARD': '0.00',
        'PAYME': '0.00',
        'UNKNOWN': '100.00',
    }
    assert row['tender_totals_source'] == 'DERIVED_INCOMPLETE_FROZEN'
    assert row['frozen_tender_evidence_complete'] is False
    assert row['tender_attribution_complete'] is False
    assert row['unattributed_expected_amount'] == '100.00'
    assert row['frozen_tender_evidence_issues'] == [
        'FROZEN_EXPECTED_MISMATCH',
        'UNATTRIBUTED_TENDER_EVIDENCE',
    ]
    assert row['frozen_tender_discrepancies'][method] == {
        'frozen': '100.00',
        'derived': '0.00',
        'derived_minus_frozen': '-100.00',
    }
    assert row['frozen_tender_discrepancies']['UNKNOWN'] == {
        'frozen': None,
        'derived': '100.00',
        'derived_minus_frozen': '100.00',
    }


@pytest.mark.parametrize('second_amount', [None, '0.00'])
def test_action_mixed_header_requires_two_positive_concrete_methods(
    second_amount,
):
    from base.models import OrderPayment
    from core.shifts.service import ShiftService

    cashier = _cashier(f'partial-mixed-{second_amount}@test.local')
    shift = _shift(cashier)
    action_id = uuid4()
    order = _paid_order(
        cashier,
        shift,
        total='100.00',
        method='MIXED',
        payment_action_id=action_id,
    )
    OrderPayment.objects.create(
        order=order,
        branch_id=shift.branch_id,
        method='CASH',
        amount='100.00',
        payment_action_id=action_id,
        line_index=0,
    )
    if second_amount is not None:
        OrderPayment.objects.create(
            order=order,
            branch_id=shift.branch_id,
            method='HUMO',
            amount=second_amount,
            payment_action_id=action_id,
            line_index=1,
        )
    _freeze(shift, {'CASH': '100.00'})

    row = ShiftService._batch_list_extras([shift])[shift.id]

    assert row['expected_by_tender']['UNKNOWN'] == '100.00'
    assert row['tender_totals_source'] == 'DERIVED_INCOMPLETE_FROZEN'
    assert row['frozen_tender_evidence_complete'] is False
    assert row['tender_attribution_complete'] is False
    assert 'UNATTRIBUTED_TENDER_EVIDENCE' in (
        row['frozen_tender_evidence_issues']
    )


def test_full_method_set_with_wrong_amount_falls_back_and_exposes_difference():
    from base.models import OrderPayment
    from core.shifts.service import ShiftService

    cashier = _cashier('wrong-frozen-amount@test.local')
    shift = _shift(cashier)
    order = _paid_order(cashier, shift, total='100.00', method='CASH')
    OrderPayment.objects.create(
        order=order,
        branch_id=shift.branch_id,
        method='CASH',
        amount='100.00',
    )
    _freeze(shift, {})

    row = ShiftService._batch_list_extras([shift])[shift.id]

    assert row['expected_by_tender']['CASH'] == '100.00'
    assert row['tender_totals_source'] == 'DERIVED_INCOMPLETE_FROZEN'
    assert row['frozen_tender_evidence_complete'] is False
    assert row['tender_attribution_complete'] is True
    assert row['frozen_tender_evidence_issues'] == [
        'FROZEN_EXPECTED_MISMATCH',
    ]
    assert row['frozen_tender_discrepancies'] == {
        'CASH': {
            'frozen': '0.00',
            'derived': '100.00',
            'derived_minus_frozen': '100.00',
        },
    }


def test_canonical_five_plus_unexpected_frozen_method_is_not_complete():
    from base.models import OrderPayment
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    cashier = _cashier('extra-frozen-method@test.local')
    shift = _shift(cashier)
    order = _paid_order(cashier, shift, total='100.00', method='CASH')
    OrderPayment.objects.create(
        order=order,
        branch_id=shift.branch_id,
        method='CASH',
        amount='100.00',
    )
    _freeze(shift, {'CASH': '100.00'})
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='MIXED',
        expected_amount='0.00',
        counted_amount='0.00',
        difference='0.00',
    )

    row = ShiftService._batch_list_extras([shift])[shift.id]

    assert row['expected_by_tender']['CASH'] == '100.00'
    assert row['tender_totals_source'] == 'DERIVED_INCOMPLETE_FROZEN'
    assert row['frozen_tender_evidence_complete'] is False
    assert row['frozen_tender_evidence_issues'] == [
        'UNEXPECTED_FROZEN_METHODS',
    ]
    assert row['frozen_tender_discrepancies']['MIXED'] == {
        'frozen': '0.00',
        'derived': None,
        'derived_minus_frozen': None,
    }


def test_frozen_complete_matches_refund_and_expense_net_semantics():
    """A valid close compares against refund-net, expense-net drawer values."""
    from base.models import OrderPayment, OrderRefund
    from cashbox.models import CashboxExpense
    from core.shifts.service import ShiftService

    cashier = _cashier('net-frozen@test.local')
    shift = _shift(cashier)
    order = _paid_order(cashier, shift, total='150.00', method='MIXED')
    OrderPayment.objects.create(
        order=order,
        branch_id=shift.branch_id,
        method='CASH',
        amount='100.00',
    )
    OrderPayment.objects.create(
        order=order,
        branch_id=shift.branch_id,
        method='HUMO',
        amount='50.00',
    )
    OrderRefund.objects.create(
        order=order,
        shift=shift,
        cashier=cashier,
        branch_id=shift.branch_id,
        amount='30.00',
        cash_amount='30.00',
        drawer_cash_amount='30.00',
        card_amount='0.00',
        payme_amount='0.00',
        unknown_amount='0.00',
        refunded_at=timezone.now(),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id='net-frozen-refund',
    )
    CashboxExpense.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        amount='20.00',
    )
    _freeze(shift, {'CASH': '50.00', 'HUMO': '50.00'})

    row = ShiftService._batch_list_extras([shift])[shift.id]

    assert row['expected_by_tender'] == {
        'CASH': '50.00',
        'UZCARD': '0.00',
        'HUMO': '50.00',
        'CARD': '0.00',
        'PAYME': '0.00',
    }
    assert row['total_expected_to_receive'] == '100.00'
    assert row['tender_totals_source'] == 'FROZEN_COMPLETE'
    assert row['frozen_tender_evidence_complete'] is True
    assert row['tender_attribution_complete'] is True
    assert row['frozen_tender_evidence_issues'] == []
    assert row['frozen_tender_discrepancies'] == {}


def test_active_snapshot_excludes_future_refund_and_expense_until_shared_now():
    from base.models import OrderPayment, OrderRefund
    from cashbox.models import CashboxExpense
    from core.shifts.service import ShiftService

    cashier = _cashier('active-snapshot@test.local')
    snapshot = timezone.now()
    shift = _shift(cashier, status='ACTIVE')
    order = _paid_order(
        cashier,
        shift,
        total='100.00',
        method='CASH',
        paid_at=snapshot - timedelta(minutes=1),
    )
    OrderPayment.objects.create(
        order=order,
        branch_id=shift.branch_id,
        method='CASH',
        amount='100.00',
    )
    future = snapshot + timedelta(minutes=10)
    OrderRefund.objects.create(
        order=order,
        shift=shift,
        cashier=cashier,
        branch_id=shift.branch_id,
        amount='20.00',
        cash_amount='20.00',
        drawer_cash_amount='20.00',
        card_amount='0.00',
        payme_amount='0.00',
        unknown_amount='0.00',
        refunded_at=future,
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id='future-active-refund',
    )
    expense = CashboxExpense.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        amount='10.00',
    )
    CashboxExpense.objects.filter(pk=expense.pk).update(created_at=future)

    early = ShiftService._batch_list_extras([shift], now=snapshot)[shift.id]
    late = ShiftService._batch_list_extras(
        [shift], now=future + timedelta(seconds=1),
    )[shift.id]
    shift.status = 'ENDED'
    shift.end_time = snapshot + timedelta(seconds=1)
    shift.save(update_fields=['status', 'end_time'])
    closed = ShiftService._batch_list_extras(
        [shift], now=snapshot,
    )[shift.id]

    assert early['expected_by_tender']['CASH'] == '100.00'
    assert early['refunds_count'] == 0
    assert early['expenses_total'] == '0.00'
    assert early['cashbox_expenses'] == []
    assert early['_live_totals']['total_revenue'] == Decimal('100.00')

    assert late['expected_by_tender']['CASH'] == '70.00'
    assert late['refunds_count'] == 1
    assert late['refunds_total'] == '20.00'
    assert late['expenses_total'] == '10.00'
    assert len(late['cashbox_expenses']) == 1
    assert late['_live_totals']['total_revenue'] == Decimal('80.00')

    # Once closed, immutable FK ownership wins over device-clock skew.
    assert closed['expected_by_tender']['CASH'] == '70.00'
    assert closed['refunds_count'] == 1
    assert closed['expenses_total'] == '10.00'
