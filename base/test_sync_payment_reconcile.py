from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import override_settings
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


def test_delivery_fee_and_tip_total_repairs_from_complete_payment_evidence(
    order_factory, settings,
):
    """Telegram total may exceed discounted item revenue by delivery/tip."""
    from base.models import Order, OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, payment, _header_time = _evidence(order_factory, settings)
    Order.objects.filter(pk=order.pk).update(
        order_type=Order.OrderType.DELIVERY,
        order_origin=Order.Origin.TELEGRAM,
        subtotal='10.00',
        discount_amount='2.00',
        total_amount='15.00',  # 8 product net + 5 delivery + 2 tip
    )
    OrderPayment.objects.filter(pk=payment.pk).update(amount='15.00')

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == {str(order.uuid)}
    assert order.is_paid is True
    assert order.total_amount == 15


def test_total_below_discounted_item_value_is_not_repaired(
    order_factory, settings,
):
    """Fee support must not weaken the item/subtotal lower-bound invariant."""
    from base.models import Order, OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    order, payment, _header_time = _evidence(order_factory, settings)
    Order.objects.filter(pk=order.pk).update(
        subtotal='10.00', discount_amount='1.00', total_amount='8.00',
    )
    OrderPayment.objects.filter(pk=payment.pk).update(amount='8.00')

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == set()
    assert order.is_paid is False


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


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch1', SYNC_ENABLED=False,
)
def test_cloud_admin_payment_pull_repairs_local_header_once(
    order_factory, settings,
):
    """A cloud-paid noncash order cannot remain collectable on its owning till."""
    from base.models import Order, OrderPayment
    from base.services.sync.service import SyncService

    order = order_factory(status='READY')
    header_time = timezone.now() - timedelta(seconds=2)
    Order.objects.filter(pk=order.pk).update(
        updated_at=header_time,
        synced_at=header_time,
    )
    order.refresh_from_db()
    payment_payload = {
        'uuid': str(uuid4()),
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': 'branch1',
        'order_uuid': str(order.uuid),
        'method': 'HUMO',
        'amount': str(order.total_amount),
        'created_at': (header_time + timedelta(seconds=1)).isoformat(),
    }

    first = SyncService._apply_records(OrderPayment, [payment_payload])

    assert first['errors'] == []
    order.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_method == 'HUMO'
    assert order.paid_at == header_time + timedelta(seconds=1)
    assert OrderPayment.objects.filter(order=order, is_deleted=False).count() == 1

    # An exact change-feed replay neither duplicates tender evidence nor makes
    # the order collectable again.
    replay = SyncService._apply_records(OrderPayment, [payment_payload])
    assert replay['errors'] == []
    order.refresh_from_db()
    assert order.is_paid is True
    assert OrderPayment.objects.filter(order=order, is_deleted=False).count() == 1


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch1', SYNC_ENABLED=False,
)
def test_cloud_payment_evidence_survives_newer_local_operational_edit(
    order_factory,
):
    """A later READY edit is not an unpay and cannot make a paid bill collectable."""
    from base.models import Order, OrderPayment
    from base.services.sync.service import SyncService

    order = order_factory(status='READY')
    local_edit_time = timezone.now()
    Order.objects.filter(pk=order.pk).update(
        updated_at=local_edit_time,
        synced_at=None,
    )
    order.refresh_from_db()
    payment_time = local_edit_time - timedelta(seconds=1)
    payload = {
        'uuid': str(uuid4()),
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': 'branch1',
        'order_uuid': str(order.uuid),
        'method': 'UZCARD',
        'amount': str(order.total_amount),
        'created_at': payment_time.isoformat(),
    }

    result = SyncService._apply_records(OrderPayment, [payload])

    assert result['errors'] == []
    order.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_method == 'UZCARD'
    assert order.paid_at == payment_time


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch1', SYNC_ENABLED=False,
)
def test_cloud_external_courier_payment_repairs_header_without_drawer_credit(
    order_factory,
):
    """Owning till consumes external evidence once but never books drawer cash."""
    from base.models import CashRegister, ExternalOrderPayment, Order
    from base.services.sync.service import SyncService

    order = order_factory(status='READY')
    Order.objects.filter(pk=order.pk).update(
        order_type=Order.OrderType.DELIVERY,
        updated_at=timezone.now(),
        synced_at=None,
    )
    order.refresh_from_db()
    register = CashRegister.objects.create(
        branch_id='branch1', current_balance='500.00',
    )
    occurred_at = timezone.now() - timedelta(seconds=1)
    payload = {
        'uuid': str(uuid4()),
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': 'branch1',
        'order_uuid': str(order.uuid),
        'source': 'COURIER',
        'source_id': 'gateway-courier-1',
        'method': 'CASH',
        'amount': str(order.total_amount),
        'occurred_at': occurred_at.isoformat(),
    }

    first = SyncService._apply_records(ExternalOrderPayment, [payload])

    assert first['errors'] == []
    order.refresh_from_db()
    register.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_method == 'CASH'
    assert order.paid_at == occurred_at
    assert register.current_balance == 500
    assert ExternalOrderPayment.objects.filter(
        order=order, source_id='gateway-courier-1', is_deleted=False,
    ).count() == 1

    replay = SyncService._apply_records(ExternalOrderPayment, [payload])
    assert replay['errors'] == []
    register.refresh_from_db()
    assert register.current_balance == 500
    assert ExternalOrderPayment.objects.filter(
        order=order, source_id='gateway-courier-1', is_deleted=False,
    ).count() == 1


@override_settings(
    DEPLOYMENT_MODE='local', BRANCH_ID='branch1', SYNC_ENABLED=False,
)
def test_external_payment_higher_version_replay_cannot_rewrite_money(
    order_factory,
):
    from base.models import ExternalOrderPayment
    from base.services.sync.service import SyncService

    order = order_factory(status='READY')
    evidence = ExternalOrderPayment.objects.create(
        order=order,
        branch_id='branch1',
        source=ExternalOrderPayment.Source.COURIER,
        source_id='immutable-event-1',
        method='CASH',
        amount='10.00',
        occurred_at=timezone.now(),
    )
    payload = evidence.to_sync_dict()
    payload.update({
        'sync_version': evidence.sync_version + 100,
        'method': 'PAYME',
        'amount': '999999.00',
        'occurred_at': (timezone.now() + timedelta(days=1)).isoformat(),
    })

    result = SyncService._apply_records(ExternalOrderPayment, [payload])

    assert result['errors'] == []
    evidence.refresh_from_db()
    assert evidence.method == 'CASH'
    assert evidence.amount == 10
    assert evidence.sync_version == 1
