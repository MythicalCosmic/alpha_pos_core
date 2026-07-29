"""Manager settlement output must prefer canonical drawer evidence."""

from datetime import timedelta

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_legacy_raw_cash_change_cannot_create_a_false_shortage(
    cashier_user,
    regular_user,
):
    from base.models import Order, OrderPayment, Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    paid_at = timezone.now()
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id='branch1',
        start_time=paid_at - timedelta(hours=1),
        end_time=paid_at + timedelta(hours=1),
        status=Shift.Status.ENDED,
        settlement_manifest={
            'version': 3,
            'cashier_counted_methods': ['CASH'],
        },
    )
    order = Order.objects.create(
        user=regular_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=paid_at,
    )
    # The customer tendered 120 and received 20 change. The drawer keeps 100.
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.CASH,
        amount='120.00',
    )
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='CASH',
        expected_amount='120.00',
        counted_amount='100.00',
        confirmed_amount='0.00',
        difference='-20.00',
    )

    settlement = ShiftService._shift_settlement(shift)

    assert settlement == [{
        'method': 'CASH',
        'expected': '100.00',
        'frozen_expected': '120.00',
        'expected_source': 'CANONICAL_DERIVED',
        'counted': '100.00',
        'cashier_count_submitted': True,
        'cashier_count_status': 'COUNTED',
        'confirmed': None,
        'manager_confirmed': False,
        'confirmation_source': None,
        'confirmation_difference': None,
        'difference': '0.00',
        'frozen_difference': '-20.00',
        'difference_source': 'CANONICAL_RECOMPUTED',
        'status': 'COUNTED',
        'reconciled': False,
        'shift_reconciled': False,
    }]
    list_cash = ShiftService._batch_list_extras([shift])[shift.pk][
        'settlement'
    ][0]
    assert list_cash == settlement[0]


def test_unsubmitted_count_is_null_not_a_zero_shortage(
    cashier_user,
):
    from base.models import Order, OrderPayment, Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    end = timezone.now()
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id='branch1',
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.ENDED,
        settlement_manifest={
            'version': 3,
            'cashier_counted_methods': [],
        },
    )
    order = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        subtotal='267000.00',
        total_amount='267000.00',
        paid_at=end - timedelta(minutes=30),
    )
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.CASH,
        amount='267000.00',
    )
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='CASH',
        expected_amount='267000.00',
        counted_amount='0.00',
        confirmed_amount='0.00',
        difference='-267000.00',
    )

    settlement = ShiftService._shift_settlement(shift)

    assert settlement[0]['status'] == 'UNCOUNTED'
    assert settlement[0]['counted'] is None
    assert settlement[0]['cashier_count_submitted'] is False
    assert settlement[0]['cashier_count_status'] == 'UNCOUNTED'
    assert settlement[0]['confirmed'] is None
    assert settlement[0]['confirmation_difference'] is None
    assert settlement[0]['difference'] is None
    assert settlement[0]['frozen_difference'] == '-267000.00'
    assert settlement[0]['difference_source'] == 'FROZEN_MATCHED'
    list_cash = ShiftService._batch_list_extras([shift])[shift.pk][
        'settlement'
    ][0]
    assert list_cash == settlement[0]


def test_reconcile_uses_same_canonical_cash_shown_to_manager(
    cashier_user,
    regular_user,
    admin_user,
):
    from base.models import CashReconciliation, Order, OrderPayment, Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    paid_at = timezone.now()
    branch = admin_user.branch_id
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id=branch,
        start_time=paid_at - timedelta(hours=1),
        end_time=paid_at + timedelta(hours=1),
        status=Shift.Status.ENDED,
    )
    order = Order.objects.create(
        user=regular_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=paid_at,
    )
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.CASH,
        amount='120.00',
    )
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='CASH',
        expected_amount='120.00',
        counted_amount='100.00',
        confirmed_amount='0.00',
        difference='-20.00',
    )

    before = ShiftService._shift_settlement(shift)[0]
    response, status = ShiftService.reconcile(
        shift.id,
        '100.00',
        '',
        admin_user.id,
        actor=admin_user,
    )

    assert status in (200, 201), response
    reconciliation = CashReconciliation.objects.get(shift=shift)
    assert reconciliation.expected_cash == 100
    assert reconciliation.actual_cash == 100
    assert reconciliation.difference == 0
    assert response['data']['expected_cash'] == before['expected'] == '100.00'
    assert response['data']['difference'] == '0.00'
    cash = response['data']['settlement'][0]
    assert cash['status'] == 'CONFIRMED'
    assert cash['expected'] == '100.00'
    assert cash['frozen_expected'] == '120.00'
    assert cash['expected_source'] == 'RECONCILIATION_FROZEN'
    assert cash['counted'] == '100.00'
    assert cash['difference'] == '0.00'
    assert cash['confirmed'] == '100.00'
    assert cash['confirmation_difference'] == '0.00'
    list_cash = ShiftService._batch_list_extras([shift])[shift.pk][
        'settlement'
    ][0]
    assert list_cash['expected'] == cash['expected']
    assert list_cash['confirmed'] == cash['confirmed']
    assert list_cash['difference'] == cash['difference']


def test_manager_confirmation_does_not_invent_a_missing_cashier_count(
    cashier_user,
    admin_user,
):
    from base.models import Order, OrderPayment, Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    end = timezone.now()
    branch = admin_user.branch_id
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id=branch,
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.ENDED,
    )
    order = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        subtotal='267000.00',
        total_amount='267000.00',
        paid_at=end - timedelta(minutes=30),
    )
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.CASH,
        amount='267000.00',
    )
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='CASH',
        expected_amount='267000.00',
        counted_amount='0.00',
        confirmed_amount='0.00',
        difference='-267000.00',
    )

    response, status = ShiftService.reconcile(
        shift.id,
        '267000.00',
        '',
        admin_user.id,
        actor=admin_user,
    )

    assert status in (200, 201), response
    cash = response['data']['settlement'][0]
    assert cash['status'] == 'CONFIRMED'
    assert cash['cashier_count_submitted'] is False
    assert cash['cashier_count_status'] == 'UNCOUNTED'
    assert cash['counted'] is None
    assert cash['difference'] is None
    assert cash['confirmed'] == '267000.00'
    assert cash['confirmation_difference'] == '0.00'
    assert cash['frozen_difference'] == '-267000.00'
    list_cash = ShiftService._batch_list_extras([shift])[shift.pk][
        'settlement'
    ][0]
    assert list_cash['cashier_count_status'] == 'UNCOUNTED'
    assert list_cash['counted'] is None
    assert list_cash['difference'] is None
    assert list_cash['confirmed'] == '267000.00'


def test_failed_tender_computation_is_unavailable_not_verified_zero(
    cashier_user,
    monkeypatch,
):
    from base.models import Order, Shift
    from core.shifts.service import ShiftService

    shift = Shift.objects.create(
        user=cashier_user,
        branch_id='branch1',
        start_time=timezone.now() - timedelta(hours=1),
        status=Shift.Status.ACTIVE,
    )

    def unavailable(*_args, **_kwargs):
        raise RuntimeError('injected tender evidence failure')

    monkeypatch.setattr(Order.objects, 'filter', unavailable)

    extras = ShiftService._batch_list_extras([shift])[shift.pk]

    assert extras['tender_totals_source'] == 'UNAVAILABLE'
    assert extras['financial_evidence_available'] is False
    assert extras['expenses_total'] is None
    assert extras['refunds_total'] is None
    assert extras['cancelled_orders_value'] is None
    assert extras['unattributed_expected_amount'] is None
    assert extras['cash_to_receive'] is None
    assert extras['noncash_to_receive'] is None
    assert extras['all_tenders_to_receive'] is None
    assert extras['total_expected_to_receive'] is None
    assert extras['tender_attribution_complete'] is False
    assert extras['frozen_tender_evidence_issues'] == [
        'EVIDENCE_UNAVAILABLE',
    ]

    serialized = ShiftService._serialize_shift(shift, extras=extras)
    assert serialized['financial_evidence_available'] is False
    assert serialized['total_revenue'] is None
    assert serialized['cash_collected'] is None
    assert serialized['net_revenue'] is None
    assert serialized['expenses_total'] is None
    assert serialized['refunds_total'] is None
    assert serialized['expected_cash'] is None
    assert serialized['expected_cash_source'] == 'UNAVAILABLE'


def test_reconciled_noncash_totals_remain_frozen_after_late_evidence(
    cashier_user,
    admin_user,
):
    from base.models import (
        CashReconciliation, Order, OrderPayment, Shift,
    )
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    end = timezone.now()
    branch = admin_user.branch_id
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id=branch,
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.COMPLETED,
    )
    first = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=branch,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.UZCARD,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=end - timedelta(minutes=30),
    )
    OrderPayment.objects.create(
        order=first,
        method=Order.PaymentMethod.UZCARD,
        amount='100.00',
    )
    cash_row = ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=branch,
        method='CASH',
        expected_amount='0.00',
        counted_amount='0.00',
        confirmed_amount='0.00',
        difference='0.00',
    )
    card_row = ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=branch,
        method='UZCARD',
        expected_amount='100.00',
        counted_amount='100.00',
        confirmed_amount='100.00',
        difference='0.00',
    )
    for method in ('HUMO', 'CARD', 'PAYME'):
        ShiftPaymentTotal.objects.create(
            shift=shift,
            branch_id=branch,
            method=method,
            expected_amount='0.00',
            counted_amount='0.00',
            confirmed_amount='0.00',
            difference='0.00',
        )
    CashReconciliation.objects.create(
        shift=shift,
        expected_cash='0.00',
        actual_cash='0.00',
        difference='0.00',
        reconciled_by=admin_user,
        treasury_posted_at=timezone.now(),
        branch_id=branch,
    )

    late = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=branch,
        display_id=2,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.UZCARD,
        subtotal='50.00',
        total_amount='50.00',
        paid_at=end - timedelta(minutes=10),
    )
    OrderPayment.objects.create(
        order=late,
        method=Order.PaymentMethod.UZCARD,
        amount='50.00',
    )

    extras = ShiftService._batch_list_extras([shift])[shift.pk]
    assert extras['tender_totals_source'] == 'RECONCILIATION_FROZEN'
    assert extras['expected_by_tender'] == {
        'CASH': '0.00',
        'UZCARD': '100.00',
        'HUMO': '0.00',
        'CARD': '0.00',
        'PAYME': '0.00',
    }
    assert extras['noncash_to_receive'] == '100.00'
    assert extras['all_tenders_to_receive'] == '100.00'
    assert extras['frozen_tender_evidence_complete'] is False
    assert extras['frozen_tender_discrepancies']['UZCARD'] == {
        'frozen': '100.00',
        'derived': '150.00',
        'derived_minus_frozen': '50.00',
    }

    list_rows = {row['method']: row for row in extras['settlement']}
    detail_rows = {
        row['method']: row for row in ShiftService._shift_settlement(shift)
    }
    for rows in (list_rows, detail_rows):
        assert rows['UZCARD']['expected'] == '100.00'
        assert rows['UZCARD']['confirmed'] == '100.00'
        assert rows['UZCARD']['expected_source'] == 'RECONCILIATION_FROZEN'
    assert cash_row.pk
    assert card_row.pk


def test_cash_reconciliation_does_not_invent_missing_noncash_bundle(
    cashier_user,
    admin_user,
):
    from base.models import (
        CashReconciliation, Order, OrderPayment, Shift,
    )
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    end = timezone.now()
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id=admin_user.branch_id,
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.COMPLETED,
    )
    order = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.HUMO,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=end - timedelta(minutes=30),
    )
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.HUMO,
        amount='100.00',
    )
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='CASH',
        expected_amount='0.00',
        counted_amount='0.00',
        confirmed_amount='0.00',
        difference='0.00',
    )
    CashReconciliation.objects.create(
        shift=shift,
        expected_cash='0.00',
        actual_cash='0.00',
        difference='0.00',
        reconciled_by=admin_user,
        branch_id=shift.branch_id,
    )

    extras = ShiftService._batch_list_extras([shift])[shift.pk]
    assert extras['cash_to_receive'] == '0.00'
    assert extras['cash_to_receive_complete'] is True
    assert extras['noncash_to_receive'] is None
    assert extras['noncash_to_receive_complete'] is False
    assert extras['all_tenders_to_receive'] is None
    assert extras['all_tenders_to_receive_complete'] is False
    assert extras['known_all_tenders_to_receive'] == '0.00'
    assert 'MISSING_FROZEN_METHODS' in extras[
        'frozen_tender_evidence_issues'
    ]


def test_legacy_cash_reconciliation_does_not_confirm_zero_noncash_rows(
    cashier_user,
    admin_user,
):
    from base.models import (
        CashReconciliation, Order, OrderPayment, Shift,
    )
    from cashbox.models import PAYMENT_METHODS, ShiftPaymentTotal
    from core.shifts.service import ShiftService

    end = timezone.now()
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id=admin_user.branch_id,
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.COMPLETED,
    )
    order = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.HUMO,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=end - timedelta(minutes=30),
    )
    OrderPayment.objects.create(
        order=order,
        method=Order.PaymentMethod.HUMO,
        amount='100.00',
    )
    for method in PAYMENT_METHODS:
        ShiftPaymentTotal.objects.create(
            shift=shift,
            branch_id=shift.branch_id,
            method=method,
            expected_amount='0.00',
            counted_amount='0.00',
            confirmed_amount='0.00',
            difference='0.00',
        )
    CashReconciliation.objects.create(
        shift=shift,
        expected_cash='0.00',
        actual_cash='0.00',
        difference='0.00',
        reconciled_by=admin_user,
        branch_id=shift.branch_id,
        # treasury_posted_at intentionally null: historical CASH-only proof.
    )

    extras = ShiftService._batch_list_extras([shift])[shift.pk]
    assert extras['cash_to_receive'] == '0.00'
    assert extras['noncash_to_receive'] is None
    assert extras['all_tenders_to_receive'] is None
    assert 'LEGACY_RECONCILIATION_CASH_ONLY' in extras[
        'frozen_tender_evidence_issues'
    ]
    assert 'FROZEN_EXPECTED_MISMATCH' in extras[
        'frozen_tender_evidence_issues'
    ]
    settlement = {
        row['method']: row for row in extras['settlement']
    }
    assert settlement['CASH']['confirmed'] == '0.00'
    assert settlement['CASH']['expected_source'] == (
        'RECONCILIATION_FROZEN'
    )
    assert settlement['HUMO']['confirmed'] is None
    assert settlement['HUMO']['manager_confirmed'] is False
    assert settlement['HUMO']['status'] == 'UNCOUNTED'
    assert settlement['HUMO']['reconciled'] is False
    assert settlement['HUMO']['shift_reconciled'] is True
    assert settlement['HUMO']['expected_source'] == (
        'LEGACY_FROZEN_UNVERIFIED'
    )
    assert extras['reconciled_count'] == 1


def test_unattributed_tender_never_becomes_verified_physical_cash(
    cashier_user,
    admin_user,
):
    from base.models import Order, Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    end = timezone.now()
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id=admin_user.branch_id,
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.ENDED,
    )
    # Legacy MIXED without any concrete lines cannot say whether this was cash
    # or non-cash. Revenue is known, but the physical split is not.
    Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.MIXED,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=end - timedelta(minutes=30),
    )
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='CASH',
        expected_amount='0.00',
        counted_amount='0.00',
        confirmed_amount='0.00',
        difference='0.00',
    )

    extras = ShiftService._batch_list_extras([shift])[shift.pk]
    assert extras['expected_by_tender']['UNKNOWN'] == '100.00'
    assert extras['tender_attribution_complete'] is False
    assert extras['cash_to_receive_complete'] is False
    assert extras['cash_to_receive'] is None
    assert extras['known_cash_to_receive'] == '0.00'
    assert extras['noncash_to_receive_complete'] is False
    assert extras['noncash_to_receive'] is None
    assert extras['all_tenders_to_receive'] == '100.00'
    assert extras['settlement'][0]['expected'] is None
    assert (
        extras['settlement'][0]['expected_source']
        == 'ATTRIBUTION_INCOMPLETE'
    )

    row = ShiftService._serialize_shift(shift, extras=extras)
    assert row['expected_cash'] is None
    assert row['expected_cash_source'] == 'ATTRIBUTION_INCOMPLETE'
    detail_cash = ShiftService._shift_settlement(shift)[0]
    assert detail_cash['expected'] is None
    assert detail_cash['difference'] is None
    assert detail_cash['expected_source'] == 'ATTRIBUTION_INCOMPLETE'

    response, status = ShiftService.reconcile(
        shift.id,
        '0.00',
        '',
        admin_user.id,
        actor=admin_user,
    )
    assert status == 422, response
    assert response['errors']['code'] == 'TENDER_ATTRIBUTION_INCOMPLETE'


def test_offsetting_unknown_sale_and_refund_remain_unattributed(
    cashier_user,
):
    from base.models import Order, OrderRefund, Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    end = timezone.now()
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id=cashier_user.branch_id,
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.ENDED,
    )
    order = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.MIXED,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=end - timedelta(minutes=30),
    )
    OrderRefund.objects.create(
        order=order,
        shift=shift,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        amount='100.00',
        cash_amount='0.00',
        drawer_cash_amount='0.00',
        card_amount='0.00',
        payme_amount='0.00',
        unknown_amount='100.00',
        refunded_at=end - timedelta(minutes=10),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id='offsetting-unknown-refund',
    )
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='CASH',
        expected_amount='0.00',
        counted_amount='0.00',
        confirmed_amount='0.00',
        difference='0.00',
    )

    extras = ShiftService._batch_list_extras([shift])[shift.pk]
    assert extras['unattributed_expected_amount'] == '0.00'
    assert extras['unattributed_evidence_count'] == 2
    assert extras['tender_attribution_complete'] is False
    assert extras['cash_to_receive'] is None
    assert extras['noncash_to_receive'] is None
    assert extras['all_tenders_to_receive'] == '0.00'
    assert extras['settlement'][0]['expected'] is None
    assert extras['settlement'][0]['difference'] is None
    assert extras['settlement'][0]['confirmation_difference'] is None
    assert extras['settlement'][0]['expected_source'] == (
        'ATTRIBUTION_INCOMPLETE'
    )
    assert extras['frozen_tender_discrepancies']['UNKNOWN'][
        'event_count'
    ] == 2


def test_unknown_refund_alone_blocks_detail_and_reconciliation(
    cashier_user,
    admin_user,
):
    from base.models import Order, OrderPayment, OrderRefund, Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    end = timezone.now()
    shift = Shift.objects.create(
        user=cashier_user,
        branch_id=admin_user.branch_id,
        start_time=end - timedelta(hours=1),
        end_time=end,
        status=Shift.Status.ENDED,
    )
    old_order = Order.objects.create(
        user=cashier_user,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        display_id=1,
        status=Order.Status.COMPLETED,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        subtotal='100.00',
        total_amount='100.00',
        paid_at=shift.start_time - timedelta(hours=1),
    )
    OrderPayment.objects.create(
        order=old_order,
        method=Order.PaymentMethod.CASH,
        amount='100.00',
    )
    OrderRefund.objects.create(
        order=old_order,
        shift=shift,
        cashier=cashier_user,
        branch_id=shift.branch_id,
        amount='100.00',
        cash_amount='0.00',
        drawer_cash_amount='0.00',
        card_amount='0.00',
        payme_amount='0.00',
        unknown_amount='100.00',
        refunded_at=end - timedelta(minutes=30),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id='unknown-refund-only',
    )
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id=shift.branch_id,
        method='CASH',
        expected_amount='0.00',
        counted_amount='0.00',
        confirmed_amount='0.00',
        difference='0.00',
    )

    detail = ShiftService._shift_settlement(shift)[0]
    assert detail['expected'] is None
    assert detail['difference'] is None
    assert detail['expected_source'] == 'ATTRIBUTION_INCOMPLETE'

    response, status = ShiftService.reconcile(
        shift.id,
        '0.00',
        '',
        admin_user.id,
        actor=admin_user,
    )
    assert status == 422, response
    assert response['errors']['code'] == 'TENDER_ATTRIBUTION_INCOMPLETE'
    assert response['errors']['unknown_refund_count'] == 1
