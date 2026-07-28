from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest
from django.apps import apps
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_legacy_multi_tender_period_aggregate_is_counted_once():
    from base.models import Inkassa

    start = timezone.now() - timedelta(hours=1)
    for method, amount in (('CASH', '30.00'), ('UZCARD', '70.00')):
        Inkassa.objects.create(
            branch_id='branch-a',
            amount=amount,
            inkass_type=method,
            balance_before='100.00',
            balance_after='70.00',
            period_start=start,
            total_orders=1,
            total_revenue='100.00',
        )

    migration = import_module('base.migrations.0042_inkassa_register_commands')
    migration.normalize_legacy_multi_tender_batches(apps, None)

    rows = list(Inkassa.objects.order_by('created_at', 'pk'))
    assert sum(row.total_orders for row in rows) == 1
    assert sum(
        (row.total_revenue for row in rows), Decimal('0'),
    ) == Decimal('100.00')
    assert rows[1].total_orders == 0
    assert rows[1].total_revenue == Decimal('0.00')
