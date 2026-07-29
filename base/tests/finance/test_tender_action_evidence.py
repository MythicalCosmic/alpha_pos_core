from decimal import Decimal
from uuid import uuid4

import pytest

from base.services.tender import (
    breakdown_sources_for_orders,
    order_tender_sources,
    split_from_rows,
    tender_integrity_issues,
    unattributed_orders,
)


ZERO_SPLIT = {
    'cash': Decimal('0.00'),
    'card': Decimal('0.00'),
    'payme': Decimal('0.00'),
    'unknown': Decimal('0.00'),
}


@pytest.mark.parametrize(
    ('method', 'bucket'),
    [
        (None, 'cash'),
        ('CASH', 'cash'),
        ('HUMO', 'card'),
        ('PAYME', 'payme'),
    ],
)
def test_actionless_legacy_header_fallback_remains_supported(method, bucket):
    split, detail = split_from_rows(Decimal('100.00'), method)

    assert split[bucket] == Decimal('100.00')
    assert split['unknown'] == Decimal('0.00')
    assert sum(split.values()) == Decimal('100.00')
    if method == 'HUMO':
        assert detail['HUMO'] == Decimal('100.00')


@pytest.mark.parametrize('method', ['CASH', 'HUMO'])
def test_positive_action_order_without_concrete_rows_fails_closed(method):
    split, _detail = split_from_rows(
        Decimal('100.00'),
        method,
        payment_action_id=uuid4(),
    )

    assert split == {**ZERO_SPLIT, 'unknown': Decimal('100.00')}


@pytest.mark.parametrize(
    ('method', 'bucket'),
    [('CASH', 'cash'), ('HUMO', 'card')],
)
def test_action_single_method_requires_matching_positive_action_row(
    method,
    bucket,
):
    action_id = uuid4()
    split, detail = split_from_rows(
        Decimal('100.00'),
        method,
        op_rows=[(method, Decimal('100.00'), action_id, 0)],
        payment_action_id=action_id,
    )

    assert split[bucket] == Decimal('100.00')
    assert split['unknown'] == Decimal('0.00')
    if method == 'HUMO':
        assert detail['HUMO'] == Decimal('100.00')


@pytest.mark.parametrize(
    'op_rows',
    [
        [('CASH', Decimal('100.00'))],
        [('HUMO', Decimal('100.00'))],
        [('CASH', Decimal('100.00')), ('HUMO', Decimal('0.00'))],
    ],
)
def test_action_mixed_dropped_or_zero_second_method_fails_closed(op_rows):
    action_id = uuid4()
    identified_rows = [
        (method, amount, action_id, index)
        for index, (method, amount) in enumerate(op_rows)
    ]

    split, _detail = split_from_rows(
        Decimal('100.00'),
        'MIXED',
        op_rows=identified_rows,
        payment_action_id=action_id,
    )

    assert split == {**ZERO_SPLIT, 'unknown': Decimal('100.00')}


def test_action_mixed_accepts_two_positive_distinct_methods():
    action_id = uuid4()
    split, detail = split_from_rows(
        Decimal('100.00'),
        'MIXED',
        op_rows=[
            ('CASH', Decimal('60.00'), action_id, 0),
            ('HUMO', Decimal('40.00'), action_id, 1),
        ],
        payment_action_id=action_id,
    )

    assert split == {
        'cash': Decimal('60.00'),
        'card': Decimal('40.00'),
        'payme': Decimal('0.00'),
        'unknown': Decimal('0.00'),
    }
    assert detail['HUMO'] == Decimal('40.00')


def test_action_method_header_must_agree_with_concrete_rows():
    action_id = uuid4()
    split, _detail = split_from_rows(
        Decimal('100.00'),
        'HUMO',
        op_rows=[('CASH', Decimal('100.00'), action_id, 0)],
        payment_action_id=action_id,
    )

    assert split == {**ZERO_SPLIT, 'unknown': Decimal('100.00')}


@pytest.mark.parametrize('amount', [Decimal('0.00'), Decimal('NaN')])
def test_action_rows_must_be_positive_and_finite(amount):
    action_id = uuid4()
    split, _detail = split_from_rows(
        Decimal('100.00'),
        'HUMO',
        op_rows=[('HUMO', amount, action_id, 0)],
        payment_action_id=action_id,
    )

    assert split == {**ZERO_SPLIT, 'unknown': Decimal('100.00')}


def test_action_external_rows_are_valid_concrete_evidence():
    action_id = uuid4()
    split, detail = split_from_rows(
        Decimal('100.00'),
        'PAYME',
        courier_rows=[('PAYME', Decimal('100.00'))],
        payment_action_id=action_id,
    )

    assert split == {
        'cash': Decimal('0.00'),
        'card': Decimal('0.00'),
        'payme': Decimal('100.00'),
        'unknown': Decimal('0.00'),
    }
    assert all(value == 0 for value in detail.values())


def test_zero_total_action_order_remains_valid_without_rows():
    split, detail = split_from_rows(
        Decimal('0.00'),
        'CASH',
        payment_action_id=uuid4(),
    )

    assert split == ZERO_SPLIT
    assert all(value == 0 for value in detail.values())


@pytest.mark.django_db
def test_direct_batch_and_integrity_paths_thread_action_identity():
    from base.models import Order, OrderPayment, User

    action_id = uuid4()
    cashier = User.objects.create(
        email=f'action-tender-{uuid4().hex}@test.local',
        first_name='Action',
        last_name='Tender',
        password='!',
        role='CASHIER',
        status='ACTIVE',
    )
    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        is_paid=True,
        subtotal=Decimal('100.00'),
        total_amount=Decimal('100.00'),
        payment_method='CASH',
        payment_action_id=action_id,
    )
    # Simulate an action header whose child identity was lost in transit.
    OrderPayment.objects.create(
        order=order,
        method='CASH',
        amount=Decimal('100.00'),
    )

    direct, _detail, drawer_cash = order_tender_sources(order)
    batch, _detail, batch_drawer_cash = breakdown_sources_for_orders(
        Order.objects.filter(pk=order.pk)
    )
    issues = tender_integrity_issues(Order.objects.filter(pk=order.pk))

    assert direct == {**ZERO_SPLIT, 'unknown': Decimal('100.00')}
    assert batch == direct
    assert drawer_cash == Decimal('0.00')
    assert batch_drawer_cash == Decimal('0.00')
    assert issues == [{
        'order_id': order.id,
        'amount': Decimal('100.00'),
        'payment_method': 'CASH',
        'reason': 'invalid or incomplete payment evidence',
    }]


@pytest.mark.django_db
def test_batch_path_accepts_complete_action_evidence():
    from base.models import Order, OrderPayment, User

    action_id = uuid4()
    cashier = User.objects.create(
        email=f'complete-tender-{uuid4().hex}@test.local',
        first_name='Complete',
        last_name='Tender',
        password='!',
        role='CASHIER',
        status='ACTIVE',
    )
    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        is_paid=True,
        subtotal=Decimal('100.00'),
        total_amount=Decimal('100.00'),
        payment_method='MIXED',
        payment_action_id=action_id,
    )
    OrderPayment.objects.bulk_create([
        OrderPayment(
            order=order,
            method='CASH',
            amount=Decimal('60.00'),
            payment_action_id=action_id,
            line_index=0,
        ),
        OrderPayment(
            order=order,
            method='HUMO',
            amount=Decimal('40.00'),
            payment_action_id=action_id,
            line_index=1,
        ),
    ])

    split, detail, drawer_cash = breakdown_sources_for_orders(
        Order.objects.filter(pk=order.pk)
    )

    assert split == {
        'cash': Decimal('60.00'),
        'card': Decimal('40.00'),
        'payme': Decimal('0.00'),
        'unknown': Decimal('0.00'),
    }
    assert detail['HUMO'] == Decimal('40.00')
    assert drawer_cash == Decimal('60.00')


@pytest.mark.django_db
def test_unattributed_canary_catches_action_cash_but_preserves_exceptions():
    from django.utils import timezone

    from base.models import ExternalOrderPayment, Order, User

    cashier = User.objects.create(
        email=f'action-canary-{uuid4().hex}@test.local',
        first_name='Action',
        last_name='Canary',
        password='!',
        role='CASHIER',
        status='ACTIVE',
    )
    missing_cash = Order.objects.create(
        user=cashier,
        cashier=cashier,
        is_paid=True,
        subtotal=Decimal('100.00'),
        total_amount=Decimal('100.00'),
        payment_method='CASH',
        payment_action_id=uuid4(),
    )
    free_order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        is_paid=True,
        subtotal=Decimal('0.00'),
        total_amount=Decimal('0.00'),
        payment_method='CASH',
        payment_action_id=uuid4(),
    )
    external_order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        is_paid=True,
        subtotal=Decimal('100.00'),
        total_amount=Decimal('100.00'),
        payment_method='PAYME',
        payment_action_id=uuid4(),
    )
    ExternalOrderPayment.objects.create(
        order=external_order,
        source=ExternalOrderPayment.Source.COURIER,
        source_id=f'action-canary-{uuid4().hex}',
        method='PAYME',
        amount=Decimal('100.00'),
        occurred_at=timezone.now(),
    )

    candidates = Order.objects.filter(
        pk__in=[missing_cash.pk, free_order.pk, external_order.pk]
    )

    assert list(unattributed_orders(candidates).values_list('pk', flat=True)) == [
        missing_cash.pk
    ]


@pytest.mark.django_db
def test_zero_total_action_with_payment_child_is_an_integrity_issue():
    from base.models import Order, OrderPayment, User

    action_id = uuid4()
    cashier = User.objects.create(
        email=f'zero-action-evidence-{uuid4().hex}@test.local',
        first_name='Zero',
        last_name='Evidence',
        password='!',
        role='CASHIER',
        status='ACTIVE',
    )
    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        is_paid=True,
        subtotal=Decimal('0.00'),
        total_amount=Decimal('0.00'),
        payment_method='CASH',
        payment_action_id=action_id,
    )
    OrderPayment.objects.create(
        order=order,
        method='CASH',
        amount=Decimal('1.00'),
        payment_action_id=action_id,
        line_index=0,
    )

    issues = tender_integrity_issues(Order.objects.filter(pk=order.pk))

    assert issues == [{
        'order_id': order.id,
        'amount': Decimal('0.00'),
        'payment_method': 'CASH',
        'reason': 'zero-total order has concrete payment evidence',
    }]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('rolled_up', 'lines'),
    [
        ('HUMO', [('HUMO', Decimal('80.00'))]),
        (
            'MIXED',
            [
                ('HUMO', Decimal('40.00')),
                ('UZCARD', Decimal('40.00')),
            ],
        ),
    ],
)
def test_action_noncash_rows_cannot_infer_an_unproven_cash_residual(
    rolled_up,
    lines,
):
    from base.models import Order, OrderPayment, User

    action_id = uuid4()
    cashier = User.objects.create(
        email=f'short-{rolled_up.lower()}-{uuid4().hex}@test.local',
        first_name='Short',
        last_name='Evidence',
        password='!',
        role='CASHIER',
        status='ACTIVE',
    )
    order = Order.objects.create(
        user=cashier,
        cashier=cashier,
        is_paid=True,
        subtotal=Decimal('100.00'),
        total_amount=Decimal('100.00'),
        payment_method=rolled_up,
        payment_action_id=action_id,
    )
    OrderPayment.objects.bulk_create([
        OrderPayment(
            order=order,
            method=method,
            amount=amount,
            payment_action_id=action_id,
            line_index=index,
        )
        for index, (method, amount) in enumerate(lines)
    ])

    orders = Order.objects.filter(pk=order.pk)
    direct, _detail, drawer_cash = order_tender_sources(order)
    batch, _detail, batch_drawer_cash = breakdown_sources_for_orders(orders)
    issues = tender_integrity_issues(orders)

    expected = {**ZERO_SPLIT, 'unknown': Decimal('100.00')}
    assert direct == expected
    assert batch == expected
    assert drawer_cash == Decimal('0.00')
    assert batch_drawer_cash == Decimal('0.00')
    assert issues == [{
        'order_id': order.id,
        'amount': Decimal('100.00'),
        'payment_method': rolled_up,
        'reason': 'invalid or incomplete payment evidence',
    }]
