from datetime import timedelta

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _evidence(order_factory, settings):
    from base.models import Order, OrderPayment

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'branch1'
    order = order_factory(status='READY')
    header_time = timezone.now() - timedelta(seconds=2)
    Order.objects.filter(pk=order.pk).update(
        updated_at=header_time,
        synced_at=header_time,
    )
    payment = OrderPayment.objects.create(
        order=order, method='CASH', amount=order.total_amount,
    )
    # Cloud publication is intentionally deferred until the surrounding
    # transaction commits.  pytest-django keeps each test in an outer
    # transaction, so establish the already-published payment evidence this
    # reconciliation unit test needs without running unrelated callbacks.
    payment.refresh_from_db()
    OrderPayment.objects.filter(pk=payment.pk).update(
        synced_at=payment.created_at + timedelta(microseconds=1),
    )
    order.refresh_from_db()
    payment.refresh_from_db()
    return order, payment, header_time


def test_payment_batch_repairs_exact_later_unpaid_header(
    order_factory, settings, monkeypatch,
):
    from base.models import CashRegister
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, payment, _ = _evidence(order_factory, settings)
    receipt_floor = timezone.now()
    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == {str(order.uuid)}
    assert order.is_paid is True
    assert order.payment_method == 'CASH'
    assert order.paid_at == payment.created_at
    # Order.save supplies the commit-order cursor even though this repair
    # intentionally keeps the original payment's economic timestamp.
    assert order.accounting_recorded_at >= receipt_floor
    assert CashRegister.objects.filter(
        branch_id=order.branch_id, is_deleted=False,
    ).exists()


def test_older_payment_cannot_resurrect_a_newer_unpay_header(
    order_factory, settings,
):
    from base.models import OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, payment, _ = _evidence(order_factory, settings)
    newer_unpay = payment.created_at + timedelta(seconds=1)
    type(order).objects.filter(pk=order.pk).update(updated_at=newer_unpay)
    OrderPayment.objects.filter(pk=payment.pk).update(
        synced_at=newer_unpay + timedelta(seconds=1),
    )

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == set()
    assert order.is_paid is False


def test_completed_order_is_covered_by_same_payment_invariant(
    order_factory, settings,
):
    from base.models import Order
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, _payment, _ = _evidence(order_factory, settings)
    Order.objects.filter(pk=order.pk).update(status=Order.Status.COMPLETED)

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == {str(order.uuid)}
    assert order.is_paid is True


def test_split_repayment_ignores_deleted_history_and_restores_mixed_header(
    order_factory, settings,
):
    from base.models import OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, old, header_time = _evidence(order_factory, settings)
    old.delete()
    cash = OrderPayment.objects.create(order=order, method='CASH', amount='4.00')
    card = OrderPayment.objects.create(order=order, method='HUMO', amount='6.00')
    OrderPayment.objects.filter(pk__in=[cash.pk, card.pk]).update(
        created_at=header_time + timedelta(seconds=1),
        synced_at=header_time + timedelta(seconds=2),
    )

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == {str(order.uuid)}
    assert order.is_paid is True
    assert order.payment_method == 'MIXED'


def test_split_tender_paid_at_is_final_payment_across_business_day_cutoff(
    order_factory, settings,
):
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from base.models import Order, OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, old, _ = _evidence(order_factory, settings)
    old.delete()

    tashkent = ZoneInfo('Asia/Tashkent')
    business_date = timezone.localdate(timezone.now(), tashkent)
    before_cutoff = datetime.combine(
        business_date, time(2, 59), tzinfo=tashkent,
    )
    after_cutoff = datetime.combine(
        business_date, time(3, 1), tzinfo=tashkent,
    )
    header_time = before_cutoff - timedelta(minutes=1)
    Order.objects.filter(pk=order.pk).update(
        updated_at=header_time,
        synced_at=header_time,
    )
    cash = OrderPayment.objects.create(order=order, method='CASH', amount='4.00')
    card = OrderPayment.objects.create(order=order, method='HUMO', amount='6.00')
    OrderPayment.objects.filter(pk=cash.pk).update(
        created_at=before_cutoff,
        synced_at=after_cutoff + timedelta(minutes=1),
    )
    OrderPayment.objects.filter(pk=card.pk).update(
        created_at=after_cutoff,
        synced_at=after_cutoff + timedelta(minutes=1),
    )

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == {str(order.uuid)}
    assert order.is_paid is True
    assert order.payment_method == 'MIXED'
    assert order.paid_at == after_cutoff
    assert timezone.localtime(order.paid_at, tashkent).date() == business_date


def test_cash_change_overtender_repairs_without_inflating_order_revenue(
    order_factory, settings,
):
    from base.models import OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, old, header_time = _evidence(order_factory, settings)
    old.delete()
    cash = OrderPayment.objects.create(
        order=order,
        method='CASH',
        amount=order.total_amount + 5,
    )
    paid_at = header_time + timedelta(seconds=1)
    OrderPayment.objects.filter(pk=cash.pk).update(
        created_at=paid_at,
        synced_at=header_time + timedelta(seconds=2),
    )

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == {str(order.uuid)}
    assert order.is_paid is True
    assert order.payment_method == 'CASH'
    assert order.paid_at == paid_at
    assert order.total_amount == 10


def test_cash_change_can_cover_only_the_residual_after_noncash(
    order_factory, settings,
):
    from base.models import OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, old, header_time = _evidence(order_factory, settings)
    old.delete()
    card = OrderPayment.objects.create(order=order, method='HUMO', amount='6.00')
    cash = OrderPayment.objects.create(order=order, method='CASH', amount='5.00')
    OrderPayment.objects.filter(pk__in=[card.pk, cash.pk]).update(
        created_at=header_time + timedelta(seconds=1),
        synced_at=header_time + timedelta(seconds=2),
    )

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == {str(order.uuid)}
    assert order.is_paid is True
    assert order.payment_method == 'MIXED'
    assert order.total_amount == 10


def test_noncash_overtender_is_not_accepted_as_payment_evidence(
    order_factory, settings,
):
    from base.models import OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, old, header_time = _evidence(order_factory, settings)
    old.delete()
    card = OrderPayment.objects.create(order=order, method='HUMO', amount='11.00')
    OrderPayment.objects.filter(pk=card.pk).update(
        created_at=header_time + timedelta(seconds=1),
        synced_at=header_time + timedelta(seconds=2),
    )

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == set()
    assert order.is_paid is False
