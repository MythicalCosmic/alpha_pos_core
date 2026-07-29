from datetime import date, datetime, time
from uuid import uuid4

import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _at(hour, minute=0):
    return timezone.make_aware(
        datetime.combine(date(2026, 7, 10), time(hour, minute)),
        timezone.get_current_timezone(),
    )


def test_repeated_local_time_filter_supports_overnight_windows():
    from base.models import Order, User
    from base.services.business_day import filter_by_repeated_local_time

    cashier = User.objects.create(
        email=f'tod-{uuid4().hex}@test.local',
        first_name='Night',
        last_name='Cashier',
        role='CASHIER',
        status='ACTIVE',
        password='!',
    )
    rows = {}
    for label, moment in (
        ('before', _at(21, 59)),
        ('late', _at(22, 0)),
        ('early', _at(1, 30)),
        ('end', _at(2, 0)),
        ('after', _at(2, 1)),
    ):
        order = Order.objects.create(
            user=cashier,
            cashier=cashier,
            status=Order.Status.COMPLETED,
            is_paid=False,
            subtotal='0',
            total_amount='0',
        )
        Order.objects.filter(pk=order.pk).update(created_at=moment)
        rows[label] = order.id

    matched = set(filter_by_repeated_local_time(
        Order.objects.filter(pk__in=rows.values()),
        time(22, 0),
        time(2, 0),
    ).values_list('id', flat=True))

    # Reporting windows are uniformly half-open [start, end): the exact end
    # boundary belongs to the next/non-operating window.
    assert matched == {rows['late'], rows['early']}


def test_tod_filter_remains_a_compatibility_alias():
    from base.services.business_day import (
        filter_by_repeated_local_time,
        tod_filter,
    )

    assert tod_filter is filter_by_repeated_local_time
