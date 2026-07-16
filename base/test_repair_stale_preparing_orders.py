from io import StringIO
import re
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _cashier(email, branch='branch1'):
    from base.models import User

    return User.objects.create(
        first_name='Queue', last_name='Cashier', email=email,
        password='x', role=User.RoleChoices.CASHIER,
        status=User.UserStatus.ACTIVE, branch_id=branch,
    )


def _order_in_shift(product, cashier, *, shift_status='ENDED',
                    shift_end_hours_ago=8, paid=True, item_ready=False):
    from base.models import Order, OrderItem, Shift

    now = timezone.now()
    end = now - timedelta(hours=shift_end_hours_ago)
    shift = Shift.objects.create(
        user=cashier,
        start_time=end - timedelta(hours=4),
        end_time=None if shift_status == 'ACTIVE' else end,
        status=shift_status,
        branch_id=cashier.branch_id,
    )
    paid_at = end - timedelta(hours=1)
    order = Order.objects.create(
        user=cashier, cashier=cashier, status=Order.Status.PREPARING,
        is_paid=paid, payment_method='CASH' if paid else None,
        paid_at=paid_at if paid else None,
        display_id=1, subtotal='10.00', total_amount='10.00',
        branch_id=cashier.branch_id,
    )
    item = OrderItem.objects.create(
        order=order, product=product, quantity=1, price='10.00',
        ready_at=paid_at if item_ready else None,
        branch_id=cashier.branch_id,
    )
    return shift, order, item


def _dry_run(**kwargs):
    out = StringIO()
    call_command('repair_stale_preparing_orders', stdout=out, **kwargs)
    text = out.getvalue()
    match = re.search(r'fingerprint: ([0-9a-f]{64})', text)
    assert match, text
    return text, match.group(1)


def test_dry_run_finds_only_closed_shift_paid_order(product, settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    cashier = _cashier('closed@test.local')
    _shift, candidate, _item = _order_in_shift(product, cashier)

    # An active-shift order is real kitchen work and must never be repaired.
    active_cashier = _cashier('active@test.local')
    _order_in_shift(
        product, active_cashier, shift_status='ACTIVE',
        shift_end_hours_ago=1,
    )
    # A just-ended shift remains inside the hand-over grace period.
    recent_cashier = _cashier('recent@test.local')
    _order_in_shift(product, recent_cashier, shift_end_hours_ago=1)
    # No matching shift means no auditable terminal boundary.
    unmatched_cashier = _cashier('unmatched@test.local')
    from base.models import Order, OrderItem
    unmatched = Order.objects.create(
        user=unmatched_cashier, cashier=unmatched_cashier,
        status='PREPARING', is_paid=True, payment_method='CASH',
        paid_at=timezone.now() - timedelta(days=2), display_id=2,
        subtotal='10.00', total_amount='10.00', branch_id='branch1',
    )
    OrderItem.objects.create(
        order=unmatched, product=product, quantity=1, price='10.00',
        branch_id='branch1',
    )

    text, _fingerprint = _dry_run(branch='branch1')

    assert 'Candidates: 1; total: 10.00' in text
    assert str(candidate.uuid) in text
    candidate.refresh_from_db()
    assert candidate.status == 'PREPARING'


def test_apply_is_guarded_and_publishes_ready_to_sync(
    product, settings, django_capture_on_commit_callbacks,
):
    settings.DEPLOYMENT_MODE = 'cloud'
    cashier = _cashier('apply@test.local')
    _shift, order, item = _order_in_shift(
        product, cashier, item_ready=True,
    )
    old_version = order.sync_version
    _text, fingerprint = _dry_run(branch='branch1')

    with django_capture_on_commit_callbacks(execute=True):
        call_command(
            'repair_stale_preparing_orders', apply=True, branch='branch1',
            expect_count=1, expect_total='10.00',
            expect_fingerprint=fingerprint,
        )

    order.refresh_from_db()
    assert order.status == 'READY'
    assert order.ready_at == item.ready_at
    assert order.sync_version == old_version + 1
    assert order.synced_at is not None


def test_apply_requires_branch_and_fresh_fingerprint(product, settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    cashier = _cashier('guards@test.local')
    _order_in_shift(product, cashier)

    with pytest.raises(CommandError, match='explicit --branch'):
        call_command(
            'repair_stale_preparing_orders', apply=True,
            expect_count=1, expect_fingerprint='0' * 64,
        )
    with pytest.raises(CommandError, match='requires --expect-count'):
        call_command(
            'repair_stale_preparing_orders', apply=True, branch='branch1',
        )
    with pytest.raises(CommandError, match='fingerprint changed'):
        call_command(
            'repair_stale_preparing_orders', apply=True, branch='branch1',
            expect_count=1, expect_fingerprint='0' * 64,
        )


def test_apply_is_forbidden_on_till(product, settings):
    settings.DEPLOYMENT_MODE = 'local'
    with pytest.raises(CommandError, match='cloud collector'):
        call_command(
            'repair_stale_preparing_orders', apply=True, branch='branch1',
            expect_count=0, expect_fingerprint='0' * 64,
        )
