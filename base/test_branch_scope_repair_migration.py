from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection
from django.test import override_settings
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _run_repair():
    migration = import_module(
        'base.migrations.0045_repair_legacy_branch_ownership'
    )
    migration.repair_legacy_branch_ownership(
        apps,
        SimpleNamespace(connection=connection),
    )


def _user(branch='branch-a'):
    from base.models import User

    return User.objects.create(
        email=f'repair-{branch}-{timezone.now().timestamp()}@test.local',
        first_name='Scope',
        last_name='Repair',
        password='!',
        role='CASHIER',
        status='ACTIVE',
        branch_id=branch,
    )


def _order(user, customer, branch):
    from base.models import Order

    return Order.objects.create(
        user=user,
        cashier=user,
        customer=customer,
        status='COMPLETED',
        branch_id=branch,
    )


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_repair_adopts_customer_only_when_order_branch_is_unambiguous():
    from base.models import Customer

    user_a = _user('branch-a')
    user_b = _user('branch-b')
    adopted = Customer.objects.create(name='Adopt', branch_id='cloud')
    ambiguous = Customer.objects.create(name='Ambiguous', branch_id='cloud')
    unused = Customer.objects.create(name='Unused', branch_id='cloud')
    _order(user_a, adopted, 'branch-a')
    _order(user_a, ambiguous, 'branch-a')
    _order(user_b, ambiguous, 'branch-b')
    published_at = timezone.now()
    Customer.objects.filter(pk=adopted.pk).update(
        sync_version=4, synced_at=published_at,
    )

    _run_repair()

    adopted.refresh_from_db()
    ambiguous.refresh_from_db()
    unused.refresh_from_db()
    assert adopted.branch_id == 'branch-a'
    assert adopted.sync_version == 5
    assert adopted.synced_at is None
    assert ambiguous.branch_id == 'cloud'
    assert unused.branch_id == 'cloud'


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_repair_inherits_shift_branch_for_legacy_payment_totals():
    from base.models import Shift
    from cashbox.models import ShiftPaymentTotal

    user = _user('branch-a')
    shift = Shift.objects.create(
        user=user,
        status='ENDED',
        start_time=timezone.now() - timedelta(hours=1),
        end_time=timezone.now(),
        branch_id='branch-a',
    )
    total = ShiftPaymentTotal.objects.create(
        shift=shift,
        method='CASH',
        expected_amount='100.00',
        counted_amount='100.00',
        confirmed_amount='100.00',
        difference='0.00',
        branch_id='cloud',
    )
    ShiftPaymentTotal.objects.filter(pk=total.pk).update(
        sync_version=2, synced_at=timezone.now(),
    )

    _run_repair()

    total.refresh_from_db()
    assert total.branch_id == 'branch-a'
    assert total.sync_version == 3
    assert total.synced_at is None
