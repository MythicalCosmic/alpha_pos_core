from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest


pytestmark = pytest.mark.django_db


OPENED = datetime(2026, 7, 1, 12, 0, tzinfo=dt_timezone.utc)
PAID = OPENED + timedelta(days=1, hours=2)


def _cross_cutoff_order(order_factory):
    from base.models import Order

    order = order_factory(status='COMPLETED', is_paid=True)
    Order.objects.filter(pk=order.pk).update(
        created_at=OPENED, paid_at=PAID, payment_method='CASH',
    )
    order.refresh_from_db()
    return order


def test_order_volume_uses_created_at_but_money_uses_paid_at(order_factory):
    from base.repositories.order import OrderRepository

    order = _cross_cutoff_order(order_factory)
    opened = OrderRepository.get_stats_aggregate(
        OPENED - timedelta(hours=1), OPENED + timedelta(hours=1),
    )
    settled = OrderRepository.get_stats_aggregate(
        PAID - timedelta(hours=1), PAID + timedelta(hours=1),
    )

    assert opened['total'] == 1
    assert opened['paid'] == 0
    assert opened['total_revenue'] == Decimal('0.00')
    assert settled['total'] == 0
    assert settled['paid'] == 1
    assert settled['total_revenue'] == order.total_amount
    assert settled['avg_order_value'] == order.total_amount


def test_daily_and_product_sales_follow_payment_business_day(order_factory):
    from base.repositories.order import OrderRepository
    from base.repositories.order_item import OrderItemRepository
    from base.services.business_day import business_date

    order = _cross_cutoff_order(order_factory)
    rows = OrderRepository.get_daily_stats(
        OPENED - timedelta(hours=1), PAID + timedelta(hours=1),
    )
    by_day = {row['date']: row for row in rows}

    assert by_day[business_date(OPENED)]['orders'] == 1
    assert by_day[business_date(OPENED)]['paid'] == 0
    assert by_day[business_date(OPENED)]['revenue'] == Decimal('0.00')
    assert by_day[business_date(PAID)]['orders'] == 0
    assert by_day[business_date(PAID)]['paid'] == 1
    assert by_day[business_date(PAID)]['revenue'] == order.total_amount

    product_id = order.items.get().product_id
    opened_products = OrderItemRepository.get_top_products(
        OPENED - timedelta(hours=1), OPENED + timedelta(hours=1),
    )
    paid_products = OrderItemRepository.get_top_products(
        PAID - timedelta(hours=1), PAID + timedelta(hours=1),
    )
    assert all(row['product_id'] != product_id for row in opened_products)
    assert any(row['product_id'] == product_id for row in paid_products)


def test_shift_notification_and_revenue_anomaly_use_payment_time(order_factory):
    from notifications.handlers.shift import ShiftNotification
    from stock.services.anomaly_service import RevenueDip
    from base.services.business_day import business_date

    order = _cross_cutoff_order(order_factory)
    opened_stats = ShiftNotification._get_shift_stats(
        OPENED - timedelta(hours=1), OPENED + timedelta(hours=1),
        cashier_id=order.cashier_id,
    )
    paid_stats = ShiftNotification._get_shift_stats(
        PAID - timedelta(hours=1), PAID + timedelta(hours=1),
        cashier_id=order.cashier_id,
    )

    assert opened_stats['total_orders'] == 1
    assert opened_stats['total_revenue'] == Decimal('0')
    assert paid_stats['total_orders'] == 0
    assert paid_stats['paid_orders'] == 1
    assert paid_stats['total_revenue'] == order.total_amount
    assert RevenueDip()._rev(business_date(OPENED)) == Decimal('0')
    assert RevenueDip()._rev(business_date(PAID)) == order.total_amount


def test_monthly_buckets_honor_business_day_cutover(order_factory):
    from base.models import Order
    from base.repositories.order import OrderRepository

    # 2026-07-01 01:00 Asia/Tashkent belongs to the June 30 business day.
    before_cutover = datetime(
        2026, 6, 30, 20, 0, tzinfo=dt_timezone.utc,
    )
    order = order_factory(status='COMPLETED', is_paid=True)
    Order.objects.filter(pk=order.pk).update(
        created_at=before_cutover,
        paid_at=before_cutover,
        payment_method='CASH',
    )

    rows = OrderRepository.get_monthly_stats()

    assert len(rows) == 1
    assert rows[0]['month'].month == 6
    assert rows[0]['orders'] == 1
    assert rows[0]['paid'] == 1
    assert rows[0]['revenue'] == Decimal(str(order.total_amount))


def test_paid_header_without_settlement_timestamp_is_not_money_event(order_factory):
    from base.repositories.order import OrderRepository

    real = _cross_cutoff_order(order_factory)
    order_factory(status='COMPLETED', is_paid=True)  # deliberately paid_at=NULL

    aggregate = OrderRepository.get_stats_aggregate()
    daily = OrderRepository.get_daily_stats()
    hourly = OrderRepository.get_hourly_distribution()

    assert aggregate['total'] == 2
    assert aggregate['paid'] == 1
    assert aggregate['total_revenue'] == real.total_amount
    assert sum(row['paid'] for row in daily) == 1
    assert sum(row['revenue'] for row in daily) == real.total_amount
    assert sum(row['revenue'] for row in hourly) == real.total_amount
