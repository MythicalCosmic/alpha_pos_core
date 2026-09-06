from io import StringIO
import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_audit_reports_missing_tender_evidence_and_can_fail(
    order_factory,
):
    from base.models import Order

    order = order_factory(status=Order.Status.COMPLETED, is_paid=True)
    Order.objects.filter(pk=order.pk).update(
        payment_method=Order.PaymentMethod.HUMO,
        paid_at=timezone.now(),
    )
    out = StringIO()

    with pytest.raises(SystemExit) as exc:
        call_command('audit_tender_attribution', '--fail', stdout=out)

    assert exc.value.code == 1
    assert 'UNATTRIBUTABLE: 1 paid order(s) worth 10.00' in out.getvalue()
    assert 'no concrete payment evidence' in out.getvalue()


def test_legacy_tender_audit_name_uses_canonical_command():
    from base.management.commands.audit_tender_attribution import (
        Command as CanonicalCommand,
    )
    from base.management.commands.check_tender_attribution import (
        Command as LegacyCommand,
    )

    assert LegacyCommand is CanonicalCommand


def test_audit_finds_old_unpaid_header_with_recent_payment_without_writes(
    order_factory,
):
    from base.models import Order, OrderPayment

    order = order_factory()
    other_branch = order_factory()
    Order.objects.filter(pk=order.pk).update(
        created_at=timezone.now() - timedelta(days=90), branch_id='audit-branch',
    )
    Order.objects.filter(pk=other_branch.pk).update(branch_id='another-branch')
    for candidate, branch in ((order, 'audit-branch'), (other_branch, 'another-branch')):
        OrderPayment.objects.create(
            order=candidate, method='UZCARD', amount='10', branch_id=branch,
        )
    before = list(Order.objects.order_by('pk').values())
    out = StringIO()
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(SystemExit) as exc:
            call_command(
                'audit_tender_attribution', '--branch', 'audit-branch',
                '--days', '60', '--json', '--fail', stdout=out,
            )

    result = json.loads(out.getvalue())
    assert exc.value.code == 1
    assert result['orders_checked'] == 1
    assert result['paid_orders_checked'] == 0
    assert result['internally_consistent'] is False
    assert result['header_issues'][0]['order_uuid'] == str(order.uuid)
    assert result['header_issues'][0]['reason'] == 'unpaid order has till payment evidence'
    assert list(Order.objects.order_by('pk').values()) == before
    assert not any(
        query['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE '))
        for query in queries
    )


def test_audit_flags_paid_order_missing_timestamp_even_if_cash_buckets_match(order_factory):
    order_factory(is_paid=True)
    out = StringIO()
    call_command('audit_tender_attribution', '--json', stdout=out)
    result = json.loads(out.getvalue())
    assert result['buckets_sum_to_revenue'] is True
    assert result['internally_consistent'] is False
    assert result['header_issues'][0]['reason'] == 'paid order has no payment timestamp'


@pytest.mark.parametrize('header_total', ['5.00', '20.00'])
def test_audit_exposes_paid_header_drift_in_both_directions(header_total, order_factory):
    from base.models import Order, OrderPayment

    order = order_factory(is_paid=True)
    Order.objects.filter(pk=order.pk).update(
        paid_at=timezone.now(), total_amount=header_total, payment_method='UZCARD',
    )
    OrderPayment.objects.create(order=order, method='UZCARD', amount='10')
    out = StringIO()
    call_command('audit_tender_attribution', '--json', stdout=out)
    result = json.loads(out.getvalue())
    assert result['internally_consistent'] is False
    assert result['tender_issues'][0]['order_id'] == order.id
    assert result['payment_breakdown']['unknown'] == header_total


def test_clean_audit_does_not_claim_physical_reconciliation(order_factory):
    from base.models import Order, OrderPayment

    order = order_factory(is_paid=True)
    Order.objects.filter(pk=order.pk).update(paid_at=timezone.now(), payment_method='CASH')
    # The extra cash can be change. This audit cannot independently establish
    # the amount physically retained or what the cashier actually handed back.
    OrderPayment.objects.create(order=order, method='CASH', amount='20')
    out = StringIO()
    call_command('audit_tender_attribution', '--json', stdout=out)
    result = json.loads(out.getvalue())
    assert result['internally_consistent'] is True
    assert result['physical_reconciliation_proven'] is False
