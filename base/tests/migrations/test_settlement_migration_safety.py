import importlib
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.apps import apps
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _user(branch):
    from base.models import User

    return User.objects.create(
        email=f'migration-{uuid4().hex}@test.local',
        first_name='Migration',
        last_name='Guard',
        password='!',
        role='ADMIN',
        status='ACTIVE',
        branch_id=branch,
    )


def _inkassa(*, user, branch, method, amount, period_start):
    from base.models import Inkassa

    return Inkassa.objects.create(
        cashier=user,
        branch_id=branch,
        amount=amount,
        inkass_type=method,
        balance_before='1000.00',
        balance_after='1000.00',
        period_start=period_start,
        total_orders=0,
        total_revenue='0.00',
    )


def _legacy_txn(*, account, amount, reference_id):
    from base.models import TreasuryTransaction

    return TreasuryTransaction.objects.create(
        account=account,
        type='INKASSA',
        delta=amount,
        balance_before='0.00',
        balance_after=amount,
        reference_type='Inkassa',
        reference_id=reference_id,
    )


def test_grandfather_groups_auto_timestamp_siblings_only_after_ledger_proof():
    from base.models import Inkassa, TreasuryAccount

    migration = importlib.import_module(
        'base.migrations.0048_unique_shift_tender_safe_post'
    )
    safe = TreasuryAccount.objects.create(kind='SAFE', balance='40.00')
    bank = TreasuryAccount.objects.create(kind='BANK', balance='60.00')
    user = _user('branch-a')
    start = timezone.now() - timedelta(hours=1)
    cash = _inkassa(
        user=user, branch='branch-a', method='CASH', amount='40.00',
        period_start=start,
    )
    card = _inkassa(
        user=user, branch='branch-a', method='HUMO', amount='60.00',
        period_start=start,
    )
    assert cash.period_end != card.period_end
    _legacy_txn(account=safe, amount='40.00', reference_id=cash.id)
    _legacy_txn(account=bank, amount='60.00', reference_id=cash.id)

    migration.grandfather_legacy_inkassa(apps, None)

    rows = list(Inkassa.objects.filter(pk__in=[cash.pk, card.pk]).order_by('pk'))
    assert [row.legacy_treasury_amount for row in rows] == [
        Decimal('40.00'), Decimal('60.00'),
    ]
    assert all(row.treasury_allocated_at is not None for row in rows)


def test_grandfather_leaves_mismatched_legacy_ledger_unstamped():
    from base.models import Inkassa, TreasuryAccount

    migration = importlib.import_module(
        'base.migrations.0048_unique_shift_tender_safe_post'
    )
    safe = TreasuryAccount.objects.create(kind='SAFE', balance='39.00')
    user = _user('branch-b')
    row = _inkassa(
        user=user, branch='branch-b', method='CASH', amount='40.00',
        period_start=timezone.now() - timedelta(hours=1),
    )
    _legacy_txn(account=safe, amount='39.00', reference_id=row.id)

    migration.grandfather_legacy_inkassa(apps, None)

    row = Inkassa.objects.get(pk=row.pk)
    assert row.legacy_treasury_amount == Decimal('0.00')
    assert row.treasury_allocated_at is None


def test_active_shift_is_not_enabled_when_created_at_overlaps_old_inkassa():
    from base.models import Inkassa, Shift

    migration = importlib.import_module(
        'base.migrations.0048_unique_shift_tender_safe_post'
    )
    user = _user('branch-overlap')
    shift = Shift.objects.create(
        user=user,
        branch_id='branch-overlap',
        status='ACTIVE',
        start_time=timezone.now() - timedelta(minutes=5),
    )
    row = _inkassa(
        user=user,
        branch='branch-overlap',
        method='CASH',
        amount='10.00',
        period_start=shift.start_time - timedelta(hours=1),
    )
    Inkassa.objects.filter(pk=row.pk).update(
        period_end=shift.start_time - timedelta(seconds=1),
    )

    migration.enable_in_progress_shifts(apps, None)

    shift.refresh_from_db()
    assert shift.treasury_settlement_eligible is False
