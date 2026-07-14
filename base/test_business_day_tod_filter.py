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


def test_tod_filter_supports_overnight_windows():
    from base.models import Order, User
    from base.services.business_day import tod_filter

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

    matched = set(tod_filter(
        Order.objects.filter(pk__in=rows.values()),
        time(22, 0),
        time(2, 0),
    ).values_list('id', flat=True))

    assert matched == {rows['late'], rows['early'], rows['end']}
