from datetime import timedelta
from decimal import Decimal
import importlib
import uuid
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _settled_order(order_factory, cashier, *, action_id=None):
    from base.models import Order

    action_id = action_id or uuid4()
    order = order_factory(cashier=cashier, status=Order.Status.PREPARING)
    paid_at = timezone.now() - timedelta(minutes=1)
    Order.objects.filter(pk=order.pk).update(
        branch_id='branch1',
        cashier=cashier,
        is_paid=True,
        payment_method=Order.PaymentMethod.CASH,
        payment_action_id=action_id,
        subtotal=Decimal('10.00'),
        discount_amount=Decimal('0.00'),
        discount_percent=Decimal('0.00'),
        total_amount=Decimal('10.00'),
        paid_at=paid_at,
    )
    order.refresh_from_db()
    return order, action_id, paid_at


def _payment_payload(order, *, action_id='missing', line_index='missing',
                     method='CASH', amount='10.00'):
    payload = {
        'uuid': str(uuid4()),
        'sync_version': 1,
        'is_deleted': False,
        'branch_id': 'branch1',
        'order_uuid': str(order.uuid),
        'method': method,
        'amount': amount,
        'created_at': timezone.now().isoformat(),
    }
    if action_id != 'missing':
        payload['payment_action_id'] = (
            str(action_id) if action_id is not None else None
        )
    if line_index != 'missing':
        payload['line_index'] = line_index
    return payload


def test_cloud_paid_order_rejects_time_travel_financial_and_cashier_rewrite(
    order_factory, cashier_user, other_cashier_user, settings,
):
    from base.models import Order
    from base.services.sync.receiver import CloudReceiver

    order, action_id, paid_at = _settled_order(order_factory, cashier_user)
    incoming = order.to_sync_dict()
    incoming.update({
        'sync_version': order.sync_version + 10,
        'is_deleted': True,
        'status': Order.Status.READY,
        'is_paid': False,
        'payment_method': Order.PaymentMethod.PAYME,
        'payment_action_id': str(uuid4()),
        'subtotal': '999.00',
        'discount_amount': '111.00',
        'discount_percent': '50.00',
        'total_amount': '888.00',
        'paid_at': (paid_at + timedelta(hours=4)).isoformat(),
        'cashier_uuid': str(other_cashier_user.uuid),
        'updated_at': timezone.now().isoformat(),
    })
    settings.DEPLOYMENT_MODE = 'cloud'

    instance, action = CloudReceiver._create_or_update(
        Order, incoming, 'branch1',
    )

    assert action == 'updated'
    instance.refresh_from_db()
    # Operational state is still allowed to converge.
    assert instance.status == Order.Status.READY
    # The accepted economic event and shift attribution are immutable.
    assert instance.is_deleted is False
    assert instance.is_paid is True
    assert instance.payment_method == Order.PaymentMethod.CASH
    assert instance.payment_action_id == action_id
    assert instance.subtotal == Decimal('10.00')
    assert instance.discount_amount == Decimal('0.00')
    assert instance.discount_percent == Decimal('0.00')
    assert instance.total_amount == Decimal('10.00')
    assert instance.paid_at == paid_at
    assert instance.cashier_id == cashier_user.id


def test_direct_sync_ingest_cannot_tombstone_a_paid_order(
    order_factory, cashier_user, settings,
):
    """The model-level ingest path enforces the same settled delete guard."""
    from base.models import Order

    order, _action_id, _paid_at = _settled_order(order_factory, cashier_user)
    incoming = order.to_sync_dict()
    incoming.update({
        'sync_version': order.sync_version + 1,
        'is_deleted': True,
        'status': Order.Status.COMPLETED,
        'updated_at': timezone.now().isoformat(),
    })
    settings.DEPLOYMENT_MODE = 'cloud'

    instance, action = Order.from_sync_dict(incoming, branch_id='branch1')

    assert action == 'updated'
    instance.refresh_from_db()
    assert instance.is_deleted is False
    assert instance.status == Order.Status.COMPLETED


def test_same_logical_payment_line_with_new_uuid_is_idempotent(
    order_factory, cashier_user, settings,
):
    from base.models import OrderPayment
    from base.services.sync.receiver import CloudReceiver

    order, action_id, _ = _settled_order(order_factory, cashier_user)
    original = OrderPayment.objects.create(
        order=order,
        branch_id='branch1',
        method='CASH',
        amount='10.00',
        payment_action_id=action_id,
        line_index=0,
    )
    settings.DEPLOYMENT_MODE = 'cloud'
    incoming = _payment_payload(
        order,
        action_id=action_id,
        line_index=0,
        amount='999.00',
    )

    instance, action = CloudReceiver._create_or_update(
        OrderPayment, incoming, 'branch1',
    )

    assert action == 'skipped'
    assert instance.pk == original.pk
    original.refresh_from_db()
    assert original.amount == Decimal('10.00')
    assert OrderPayment.objects.filter(order=order, is_deleted=False).count() == 1


def test_branch_pull_rejects_payment_from_other_action(
    order_factory, cashier_user, settings,
):
    """The cloud change-feed path cannot duplicate a local settled checkout."""
    from base.models import OrderPayment
    from base.services.sync.service import SyncService

    order, action_id, _ = _settled_order(order_factory, cashier_user)
    OrderPayment.objects.create(
        order=order,
        branch_id='branch1',
        method='CASH',
        amount='10.00',
        payment_action_id=action_id,
        line_index=0,
    )
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'branch1'
    settings.SYNC_ENABLED = False
    incoming = _payment_payload(
        order,
        action_id=uuid4(),
        line_index=0,
    )

    result = SyncService._apply_records(OrderPayment, [incoming])

    assert result['errors'] == []
    assert result['skipped'] == 1
    assert OrderPayment.objects.filter(order=order, is_deleted=False).count() == 1


@pytest.mark.parametrize('incoming_action,incoming_index', [
    ('missing', 'missing'),
    (None, None),
    ('different', 1),
])
def test_settled_action_rejects_anonymous_or_different_payment_rows(
    order_factory, cashier_user, settings, incoming_action, incoming_index,
):
    from base.models import OrderPayment
    from base.services.sync.receiver import CloudReceiver

    order, action_id, _ = _settled_order(order_factory, cashier_user)
    OrderPayment.objects.create(
        order=order,
        branch_id='branch1',
        method='CASH',
        amount='10.00',
        payment_action_id=action_id,
        line_index=0,
    )
    if incoming_action == 'different':
        incoming_action = uuid4()
    settings.DEPLOYMENT_MODE = 'cloud'
    incoming = _payment_payload(
        order,
        action_id=incoming_action,
        line_index=incoming_index,
    )

    instance, action = CloudReceiver._create_or_update(
        OrderPayment, incoming, 'branch1',
    )

    assert instance is None
    assert action == 'skipped'
    assert OrderPayment.objects.filter(order=order, is_deleted=False).count() == 1


def test_legacy_actionless_split_remains_accepted_during_rolling_upgrade(
    order_factory, cashier_user, settings,
):
    from base.models import Order, OrderPayment
    from base.services.sync.receiver import CloudReceiver

    order = order_factory(cashier=cashier_user, status=Order.Status.READY)
    Order.objects.filter(pk=order.pk).update(
        branch_id='branch1', is_paid=True, payment_method=Order.PaymentMethod.MIXED,
    )
    order.refresh_from_db()
    settings.DEPLOYMENT_MODE = 'cloud'

    cash, cash_action = CloudReceiver._create_or_update(
        OrderPayment,
        _payment_payload(order, method='CASH', amount='4.00'),
        'branch1',
    )
    card, card_action = CloudReceiver._create_or_update(
        OrderPayment,
        _payment_payload(order, method='HUMO', amount='6.00'),
        'branch1',
    )

    assert cash_action == card_action == 'created'
    assert cash.payment_action_id is None and cash.line_index is None
    assert card.payment_action_id is None and card.line_index is None
    assert OrderPayment.objects.filter(order=order, is_deleted=False).count() == 2


def test_database_rejects_duplicate_live_action_line_but_allows_legacy_rows(
    order_factory, cashier_user,
):
    from base.models import OrderPayment

    order, action_id, _ = _settled_order(order_factory, cashier_user)
    OrderPayment.objects.create(
        order=order, method='CASH', amount='10.00',
        payment_action_id=action_id, line_index=0,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        OrderPayment.objects.create(
            order=order, method='CASH', amount='10.00',
            payment_action_id=action_id, line_index=0,
        )

    # NULL action/index is the intentional compatibility lane for old clients.
    OrderPayment.objects.create(order=order, method='CASH', amount='1.00')
    OrderPayment.objects.create(order=order, method='CASH', amount='1.00')


def test_migration_backfills_only_complete_legacy_tender_evidence(
    order_factory, cashier_user,
):
    from django.apps import apps
    from base.models import Order, OrderPayment

    complete = order_factory(cashier=cashier_user, status=Order.Status.READY)
    incomplete = order_factory(cashier=cashier_user, status=Order.Status.READY)
    paid_at = timezone.now() - timedelta(minutes=1)
    Order.objects.filter(pk__in=[complete.pk, incomplete.pk]).update(
        branch_id='branch1',
        is_paid=True,
        payment_method=Order.PaymentMethod.MIXED,
        paid_at=paid_at,
        payment_action_id=None,
    )
    complete_lines = [
        OrderPayment.objects.create(
            order=complete, branch_id='branch1', method='CASH', amount='4.00',
        ),
        OrderPayment.objects.create(
            order=complete, branch_id='branch1', method='HUMO', amount='6.00',
        ),
    ]
    # MIXED promises another component and 4 does not cover a 10 bill. This
    # order must stay in the actionless compatibility lane for a late old line.
    incomplete_line = OrderPayment.objects.create(
        order=incomplete, branch_id='branch1', method='CASH', amount='4.00',
    )
    migration = importlib.import_module(
        'base.migrations.0053_payment_action_identity',
    )

    migration.backfill_payment_actions(apps, schema_editor=None)

    complete.refresh_from_db()
    incomplete.refresh_from_db()
    for payment in [*complete_lines, incomplete_line]:
        payment.refresh_from_db()
    expected_action = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f'https://alphapos.uz/payment-action/{complete.uuid}',
    )
    assert complete.payment_action_id == expected_action
    assert [line.payment_action_id for line in complete_lines] == [
        expected_action, expected_action,
    ]
    assert [line.line_index for line in complete_lines] == [0, 1]
    assert incomplete.payment_action_id is None
    assert incomplete_line.payment_action_id is None
    assert incomplete_line.line_index is None


def test_reconciliation_carries_consistent_payment_action_to_header(
    order_factory, settings,
):
    from base.models import Order, OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory(status=Order.Status.READY)
    action_id = uuid4()
    header_time = timezone.now() - timedelta(seconds=2)
    Order.objects.filter(pk=order.pk).update(
        branch_id='branch1',
        updated_at=header_time,
        synced_at=header_time,
    )
    payment = OrderPayment.objects.create(
        order=order,
        branch_id='branch1',
        method='CASH',
        amount=order.total_amount,
        payment_action_id=action_id,
        line_index=0,
    )
    OrderPayment.objects.filter(pk=payment.pk).update(
        synced_at=payment.created_at + timedelta(microseconds=1),
    )

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == {str(order.uuid)}
    assert order.is_paid is True
    assert order.payment_action_id == action_id


def test_reconciliation_refuses_mixed_payment_actions(
    order_factory, settings,
):
    from base.models import Order, OrderPayment
    from base.services.order_payment_reconciliation import (
        reconcile_stale_paid_headers,
    )

    settings.DEPLOYMENT_MODE = 'cloud'
    order = order_factory(status=Order.Status.READY)
    header_time = timezone.now() - timedelta(seconds=2)
    Order.objects.filter(pk=order.pk).update(
        branch_id='branch1',
        updated_at=header_time,
        synced_at=header_time,
    )
    payments = [
        OrderPayment.objects.create(
            order=order, branch_id='branch1', method='CASH', amount='4.00',
            payment_action_id=uuid4(), line_index=0,
        ),
        OrderPayment.objects.create(
            order=order, branch_id='branch1', method='HUMO', amount='6.00',
            payment_action_id=uuid4(), line_index=0,
        ),
    ]
    OrderPayment.objects.filter(pk__in=[p.pk for p in payments]).update(
        synced_at=timezone.now(),
    )

    repaired = reconcile_stale_paid_headers([order.id])

    order.refresh_from_db()
    assert repaired == set()
    assert order.is_paid is False
    assert order.payment_action_id is None
