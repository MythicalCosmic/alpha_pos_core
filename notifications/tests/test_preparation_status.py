from datetime import timedelta
import importlib

import pytest
from django.utils import timezone

from notifications.preparation import (
    GREEN,
    RED,
    YELLOW,
    classify_preparation,
    preparation_target_for_name,
    preparation_target_for_order,
)


@pytest.mark.parametrize(
    ('name', 'seconds', 'display'),
    [
        ('Hot Dog mini', 180, '3 daqiqa'),
        ('Hot Dog dabl', 240, '4 daqiqa'),
        ('Hot Dog karalevskiy', 240, '4 daqiqa'),
        ('Non burger standart', 360, '6 daqiqa'),
        ('Non burger tovuq sirli', 360, '6 daqiqa'),
        ('Longer', 300, '5 daqiqa'),
        ('LONGER CHIZ', 300, '5 daqiqa'),
        ('Toster', 300, '5 daqiqa'),
        ('Toster chiz', 300, '5 daqiqa'),
        ('Chicken burger', 480, '8 daqiqa'),
        ('Chicken chiz burger', 480, '8 daqiqa'),
        ('Burger donarli', 480, '8 daqiqa'),
        ('Burger', 1200, '20 daqiqa'),
        ('Pitsa pepperoni katta', 1200, '15–20 daqiqa'),
        ('Asarti long pitsa combo', 1200, '15–20 daqiqa'),
        ('Kartoshka fri', 180, '3 daqiqa'),
        ('Smart strips 8ta', 480, '7–8 daqiqa'),
        ('Qanotcha 5ta', 540, '8–9 daqiqa'),
        ('Strips 10ta', 480, '7–8 daqiqa'),
        ('Naggetsi 6ta', 360, '5–6 daqiqa'),
        ('File 10ta', 480, '7–8 daqiqa'),
        ('Chicken big', 660, '10–11 daqiqa'),
    ],
)
def test_live_catalog_names_resolve_to_approved_targets(name, seconds, display):
    target = preparation_target_for_name(name)

    assert target.maximum_seconds == seconds
    assert target.display == display


def test_unknown_products_are_not_assigned_an_invented_target():
    assert preparation_target_for_name('Coca Cola 0.5') is None


def test_mixed_order_uses_its_slowest_tracked_product():
    target = preparation_target_for_order([
        'Hot Dog mini',
        'Non burger standart',
        'Pitsa pepperoni katta',
    ])

    assert target.maximum_seconds == 20 * 60
    assert target.display == '15–20 daqiqa'


@pytest.mark.parametrize(
    ('elapsed_seconds', 'expected'),
    [
        (3 * 60, GREEN),
        (6 * 60, GREEN),
        (6 * 60 + 1, YELLOW),
        (9 * 60, YELLOW),
        (9 * 60 + 1, RED),
        (15 * 60, RED),
        (20 * 60, RED),
    ],
)
def test_non_burger_severity_boundaries(elapsed_seconds, expected):
    target = preparation_target_for_name('Non burger standart')

    assert classify_preparation(elapsed_seconds, target) == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('elapsed_minutes', 'icon', 'level'),
    [
        (4, '🟢', 'ON_TIME'),
        (8, '🟡', 'SLIGHTLY_LATE'),
        (16, '🔴', 'VERY_LATE'),
    ],
)
def test_ready_handler_sends_colored_actual_and_target_time(
    settings,
    monkeypatch,
    elapsed_minutes,
    icon,
    level,
):
    from base.models import Category, Order, OrderItem, Product, User
    from notifications.handlers.order import OrderNotification
    from notifications.services.sender_service import SenderService

    settings.EDITION = 'server'
    user = User.objects.create(
        email=f'prep-{elapsed_minutes}@example.com',
        password='x',
        role='CASHIER',
        status='ACTIVE',
    )
    category = Category.objects.create(name=f'Non burger {elapsed_minutes}')
    product = Product.objects.create(
        name='Non burger standart',
        price='10000.00',
        category=category,
    )
    order = Order.objects.create(
        user=user,
        cashier=user,
        status=Order.Status.READY,
        subtotal='10000.00',
        total_amount='10000.00',
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        quantity=1,
        price='10000.00',
    )
    ready_at = timezone.now()
    Order.objects.filter(pk=order.pk).update(
        created_at=ready_at - timedelta(minutes=elapsed_minutes),
        ready_at=ready_at,
    )
    captured = []
    monkeypatch.setattr(
        SenderService,
        'send',
        classmethod(
            lambda cls, notification_type, context, **kwargs:
            captured.append((notification_type, context, kwargs)) or True
        ),
    )

    assert OrderNotification.on_order_ready(order.id) is True

    notification_type, context, kwargs = captured[0]
    assert notification_type == 'order.ready'
    assert kwargs == {'order_id': order.id, 'thread_role': 'edit'}
    assert context['prep_elapsed'] == f'{elapsed_minutes}:00'
    assert context['prep_target'] == '6 daqiqa'
    assert context['prep_status_icon'] == icon
    assert context['prep_status_level'] == level
    assert context['prep_time'].startswith(f'{icon} {elapsed_minutes}:00')
    assert "me'yor 6 daqiqa" in context['prep_time']


@pytest.mark.django_db
def test_template_migration_updates_only_the_untouched_default():
    from notifications.models import NotificationTemplate

    migration = importlib.import_module(
        'notifications.migrations.0013_order_ready_preparation_status',
    )
    apps = importlib.import_module('django.apps').apps
    row, _ = NotificationTemplate.objects.update_or_create(
        notification_type='order.ready',
        defaults=migration.PREVIOUS_TEMPLATE,
    )

    migration.apply_template(apps, None)
    row.refresh_from_db()
    assert row.template_text == migration.NEW_TEMPLATE['template_text']

    custom = '<b>My custom ready message: {prep_time}</b>'
    row.template_text = custom
    row.save(update_fields=['template_text', 'updated_at'])
    migration.restore_template(apps, None)
    row.refresh_from_db()
    assert row.template_text == custom
