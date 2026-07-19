import pytest
from django.db import transaction

from base.models import Order, User
from core.realtime import signals


@pytest.mark.django_db(transaction=True)
def test_order_realtime_event_is_emitted_only_after_commit(monkeypatch):
    events = []
    monkeypatch.setattr(
        signals,
        'publish_order_event',
        lambda event, payload: events.append((event, payload)),
    )
    user = User.objects.create(
        email='realtime-commit@test.local',
        password='!',
        role=User.RoleChoices.CASHIER,
        status='ACTIVE',
        branch_id='main',
    )

    with transaction.atomic():
        order = Order.objects.create(
            user=user,
            cashier=user,
            branch_id='main',
            status=Order.Status.READY,
            order_origin=Order.Origin.TELEGRAM,
            is_paid=False,
            subtotal='127000.00',
            total_amount='127000.00',
        )
        assert events == []

    assert len(events) == 1
    assert events[0][0] == 'created'
    assert events[0][1]['is_paid'] is False
    assert events[0][1]['order_origin'] == Order.Origin.TELEGRAM

    with pytest.raises(RuntimeError):
        with transaction.atomic():
            order.is_paid = True
            order.payment_method = Order.PaymentMethod.CASH
            order.save(update_fields=['is_paid', 'payment_method'])
            assert len(events) == 1
            raise RuntimeError('simulate later drawer/stock failure')

    # The rolled-back payment save must never escape over the realtime channel.
    assert len(events) == 1
    order.refresh_from_db()
    assert order.is_paid is False
