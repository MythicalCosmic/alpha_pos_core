from decimal import Decimal

from base.services.tender import split_from_rows


def test_till_and_courier_rows_are_attributed_together():
    split, detail = split_from_rows(
        Decimal('100000'),
        'MIXED',
        op_rows=[('CASH', Decimal('40000'))],
        courier_rows=[('PAYME', Decimal('60000'))],
        order_id=1,
    )

    assert split == {
        'cash': Decimal('40000'),
        'card': Decimal('0'),
        'payme': Decimal('60000'),
        'unknown': Decimal('0'),
    }
    assert all(value == 0 for value in detail.values())


def test_combined_noncash_sources_cannot_exceed_order_total():
    split, _detail = split_from_rows(
        Decimal('100000'),
        'MIXED',
        op_rows=[('UZCARD', Decimal('60000'))],
        courier_rows=[('PAYME', Decimal('40001'))],
        order_id=1,
    )

    assert split['unknown'] == Decimal('100000')
    assert sum(split.values()) == Decimal('100000')


def test_missing_payment_child_is_not_guessed_as_cash():
    split, _detail = split_from_rows(
        Decimal('100000'),
        'MIXED',
        op_rows=[('UZCARD', Decimal('60000'))],
        order_id=1,
    )

    assert split == {
        'cash': Decimal('0'),
        'card': Decimal('0'),
        'payme': Decimal('0'),
        'unknown': Decimal('100000'),
    }


def test_cash_tender_may_exceed_but_must_cover_bill_residual():
    split, _detail = split_from_rows(
        Decimal('50000'),
        'MIXED',
        op_rows=[
            ('UZCARD', Decimal('20000')),
            ('CASH', Decimal('35000')),
        ],
        order_id=1,
    )

    assert split['card'] == Decimal('20000')
    assert split['cash'] == Decimal('30000')
    assert split['unknown'] == Decimal('0')


def test_external_cash_is_exact_and_cannot_hide_as_till_change():
    split, _detail = split_from_rows(
        Decimal('100000'),
        'MIXED',
        op_rows=[('UZCARD', Decimal('60000'))],
        courier_rows=[('CASH', Decimal('50000'))],
        order_id=1,
    )

    assert split['unknown'] == Decimal('100000')
    assert sum(split.values()) == Decimal('100000')
