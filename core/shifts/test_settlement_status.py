from datetime import timedelta

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _cashier(email):
    from base.models import User

    return User.objects.create(
        email=email,
        first_name='Cashier',
        password='!',
        role='CASHIER',
        status='ACTIVE',
        branch_id='branch1',
    )


def _paid_order(cashier, shift, *, method, amount):
    from base.models import Order, OrderPayment

    now = timezone.now()
    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        branch_id='branch1',
        status=Order.Status.READY,
        is_paid=True,
        payment_method=method,
        paid_at=now,
        subtotal=amount,
        total_amount=amount,
    )
    OrderPayment.objects.create(
        order=order,
        branch_id='branch1',
        method=method,
        amount=amount,
    )
    assert shift.start_time < order.paid_at
    return order


def test_legacy_zero_count_is_unsubmitted_not_full_shortage():
    """Production shift 102 shape: HUMO exists, but no blind count was sent."""
    from base.models import Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService

    cashier = _cashier('legacy-unsubmitted-count@test.local')
    now = timezone.now()
    shift = Shift.objects.create(
        user=cashier,
        branch_id='branch1',
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(seconds=1),
        status=Shift.Status.ENDED,
        total_orders=1,
        total_revenue='602000.00',
        cash_collected='0.00',
        settlement_manifest={},
    )
    _paid_order(cashier, shift, method='HUMO', amount='602000.00')
    ShiftPaymentTotal.objects.create(
        shift=shift,
        branch_id='branch1',
        method='HUMO',
        expected_amount='602000.00',
        counted_amount='0.00',
        difference='-602000.00',
    )

    response, status = ShiftService.get(shift.id, actor=cashier)

    assert status == 200, response
    data = response['data']
    assert data['card_collected'] == '602000.00'
    assert data['expected_by_tender']['HUMO'] == '602000.00'
    assert data['expected_by_tender']['CASH'] == '0.00'
    assert data['total_expected_to_receive'] == '602000.00'
    assert data['tender_totals_source'] == 'DERIVED_INCOMPLETE_FROZEN'
    assert data['frozen_tender_evidence_complete'] is False
    humo = next(row for row in data['settlement'] if row['method'] == 'HUMO')
    assert humo['counted'] == '0.00'
    assert humo['status'] == 'UNCOUNTED'


@pytest.mark.parametrize(
    ('counted', 'expected_marker', 'expected_status'),
    [
        (None, [], 'UNCOUNTED'),
        ({'CASH': '0.00'}, ['CASH'], 'COUNTED'),
    ],
)
def test_close_manifest_distinguishes_missing_from_explicit_zero_count(
    counted, expected_marker, expected_status,
):
    from base.models import Shift
    from cashbox.models import ShiftPaymentTotal
    from core.shifts.service import ShiftService, _settlement_bundle_error

    suffix = 'missing' if counted is None else 'explicit-zero'
    cashier = _cashier(f'{suffix}@test.local')
    shift = Shift.objects.create(
        user=cashier,
        branch_id='branch1',
        start_time=timezone.now() - timedelta(hours=1),
        status=Shift.Status.ACTIVE,
        treasury_settlement_eligible=True,
    )
    _paid_order(cashier, shift, method='CASH', amount='100.00')

    response, status = ShiftService.end_shift(
        shift.id,
        cashier.id,
        notes='',
        counted=counted,
        actor=cashier,
    )

    assert status == 200, response
    shift.refresh_from_db()
    assert shift.settlement_manifest['cashier_counted_methods'] == expected_marker
    cash = next(
        row for row in response['data']['settlement']
        if row['method'] == 'CASH'
    )
    assert cash['counted'] == '0.00'
    assert cash['status'] == expected_status
    frozen = list(ShiftPaymentTotal.objects.filter(shift=shift))
    assert _settlement_bundle_error(shift, frozen) is None
