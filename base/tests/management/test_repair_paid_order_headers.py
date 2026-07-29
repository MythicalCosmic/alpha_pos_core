from io import StringIO
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _stale_header(order_factory, settings, amount='10.00', method='CASH'):
    from base.models import OrderPayment

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_ID = 'branch1'
    order = order_factory(status='READY')
    header_published_at = timezone.now() - timedelta(seconds=1)
    type(order).objects.filter(pk=order.pk).update(
        synced_at=header_published_at,
    )
    payment = OrderPayment.objects.create(
        order=order, method=method, amount=amount,
    )
    OrderPayment.objects.filter(pk=payment.pk).update(
        synced_at=header_published_at + timedelta(microseconds=1),
    )
    order.refresh_from_db()
    payment.refresh_from_db()
    return order, payment


def test_command_is_dry_run_by_default(order_factory, settings):
    order, _payment = _stale_header(order_factory, settings)
    out = StringIO()

    call_command('repair_paid_order_headers', stdout=out)

    order.refresh_from_db()
    assert order.is_paid is False
    assert 'Candidates: 1; total: 10.00' in out.getvalue()
    assert 'Dry-run only' in out.getvalue()


def test_apply_repairs_only_proven_header_and_publishes_it(
    order_factory, settings, django_capture_on_commit_callbacks,
):
    order, payment = _stale_header(order_factory, settings, method='HUMO')
    old_version = order.sync_version
    out = StringIO()

    with django_capture_on_commit_callbacks(execute=True):
        call_command(
            'repair_paid_order_headers',
            apply=True,
            branch='branch1',
            expect_count=1,
            expect_total='10.00',
            stdout=out,
        )

    order.refresh_from_db()
    assert order.is_paid is True
    assert order.payment_method == 'HUMO'
    assert order.paid_at == payment.created_at
    assert order.sync_version == old_version + 1
    assert order.synced_at is not None
    assert 'Repaired 1 order header(s), 10.00 total.' in out.getvalue()


def test_mismatched_tender_is_not_a_candidate(order_factory, settings):
    order, _payment = _stale_header(order_factory, settings, amount='9.00')
    out = StringIO()

    call_command('repair_paid_order_headers', stdout=out)

    order.refresh_from_db()
    assert order.is_paid is False
    assert 'Candidates: 0; total: 0.00' in out.getvalue()


def test_fingerprint_guard_aborts_without_changes(order_factory, settings):
    order, _payment = _stale_header(order_factory, settings)

    with pytest.raises(CommandError, match='fingerprint changed'):
        call_command(
            'repair_paid_order_headers',
            apply=True,
            expect_fingerprint='0' * 64,
        )

    order.refresh_from_db()
    assert order.is_paid is False
