"""Regression coverage for AI money/identity/branch accounting semantics."""
from datetime import datetime, time, timedelta
from decimal import Decimal
import json
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone

from base.models import (
    CashRegister, Category, Customer, Order, OrderItem, OrderPayment,
    OrderRefund, Product, Shift, User,
)
from base.repositories.order import OrderRepository
from base.repositories.order_item import OrderItemRepository
from base.services.business_day import business_date, resolve_reporting_window
from stock.models import (
    AIBriefing, ProductComponentStock, ProductStockLink, PurchaseOrder, Recipe,
    RecipeIngredient, StockItem, StockLevel, StockLocation, StockTransaction,
    StockUnit, Supplier,
)
from stock.services.ai_assistant_service import AIStockAssistant
from stock.services.ai_briefing_service import AIBriefingService
from stock.services.ai_tools_service import AIToolbox
from stock.services.product_link_service import ProductStockLinkService
from stock.services.recipe_service import RecipeService


pytestmark = pytest.mark.django_db


def test_ai_tool_date_parser_accepts_iso_dates():
    from stock.services.ai_tools_service import _parse_date

    assert _parse_date('2026-07-13') == datetime(2026, 7, 13).date()
    assert _parse_date('not-a-date') is None


def test_morning_briefing_cache_is_location_scoped(monkeypatch):
    user = _user('briefing@test.local')
    a = StockLocation.objects.create(name='A', branch_id='branch-a')
    b = StockLocation.objects.create(name='B', branch_id='branch-b')
    monkeypatch.setattr(
        AIBriefingService,
        '_compose',
        classmethod(lambda cls, location_id=None: [{'location': location_id}]),
    )

    first = AIBriefingService.get_or_generate(user.id, a.id)
    same = AIBriefingService.get_or_generate(user.id, str(a.id))
    second = AIBriefingService.get_or_generate(user.id, b.id)

    assert same['id'] == first['id']
    assert first['id'] != second['id']
    assert first['location_id'] == a.id
    assert second['location_id'] == b.id
    assert first['bullets'] != second['bullets']


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_morning_briefing_rejects_invalid_location_before_cache_access():
    user = _user('briefing-invalid@test.local')

    malformed = AIBriefingService.get_or_generate(user.id, 'bad-location')
    missing = AIBriefingService.get_or_generate(user.id, 999999)

    assert malformed['error'] == 'invalid_location'
    assert missing['error'] == 'invalid_location'
    assert AIBriefing.objects.count() == 0


def _user(email='money@test.local'):
    return User.objects.create(
        email=email, first_name='Money', last_name='Tester',
        role=User.RoleChoices.CASHIER, status=User.UserStatus.ACTIVE,
        password='test-hash',
    )


def _order(user, *, branch='branch-a', total='100', subtotal=None,
           discount='0', paid=True, status=Order.Status.READY,
           paid_at=None, created_at=None, display_id=1, order_number=None):
    subtotal = subtotal if subtotal is not None else total
    order = Order.objects.create(
        user=user, cashier=user, branch_id=branch, status=status,
        is_paid=paid, payment_method='CASH' if paid else None,
        paid_at=paid_at if paid else None, display_id=display_id,
        order_number=order_number, subtotal=Decimal(str(subtotal)),
        discount_amount=Decimal(str(discount)), total_amount=Decimal(str(total)),
    )
    if created_at is not None:
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()
    return order


def _local_at(day, hour, minute=0):
    return timezone.make_aware(
        datetime.combine(day, time(hour, minute)),
        timezone.get_current_timezone(),
    )


def _freeze_at_open_business_time(monkeypatch):
    """Keep current-period assertions independent of the 03:00-07:00 gap."""
    frozen_now = _local_at(business_date(), 12)
    monkeypatch.setattr(timezone, 'now', lambda: frozen_now)
    return frozen_now - timedelta(minutes=1)


def _stock_units():
    gram = StockUnit.objects.create(
        name='Gram', short_name='g', unit_type=StockUnit.UnitType.WEIGHT,
        is_base_unit=True, decimal_places=4,
    )
    kilogram = StockUnit.objects.create(
        name='Kilogram', short_name='kg', unit_type=StockUnit.UnitType.WEIGHT,
        base_unit=gram, conversion_factor='1000', decimal_places=4,
    )
    piece = StockUnit.objects.create(
        name='Piece', short_name='pc', unit_type=StockUnit.UnitType.COUNT,
        is_base_unit=True, decimal_places=4,
    )
    return gram, kilogram, piece


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_multiday_ai_and_repository_reports_exclude_quiet_gap_boundaries():
    """04:00 between selected business dates is not reportable; 07:00 is."""
    user = _user('operating-window@test.local')
    location = StockLocation.objects.create(
        name='Operating window', type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-a',
    )
    category = Category.objects.create(name='Window food', branch_id='branch-a')
    product = Product.objects.create(
        name='Boundary burger', category=category, price='100',
        branch_id='branch-a',
    )
    date_from = business_date() - timedelta(days=2)
    date_to = date_from + timedelta(days=1)

    quiet = _local_at(date_to, 4)
    opened = _local_at(date_to, 7)
    quiet_order = _order(
        user, total='900', created_at=quiet, paid_at=quiet, display_id=20,
    )
    opened_order = _order(
        user, total='100', created_at=opened, paid_at=opened, display_id=21,
    )
    OrderItem.objects.create(
        order=quiet_order, product=product, quantity=9, price='100',
        branch_id='branch-a',
    )
    OrderItem.objects.create(
        order=opened_order, product=product, quantity=1, price='100',
        branch_id='branch-a',
    )
    Shift.objects.create(
        user=user, branch_id='branch-a', status=Shift.Status.ENDED,
        start_time=quiet, end_time=quiet + timedelta(minutes=30),
    )
    opened_shift = Shift.objects.create(
        user=user, branch_id='branch-a', status=Shift.Status.ENDED,
        start_time=opened, end_time=opened + timedelta(minutes=30),
    )

    args = {
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
    }
    listed = json.loads(AIToolbox.execute('list_orders', args, location.id))
    listed_shifts = json.loads(AIToolbox.execute('list_shifts', args, location.id))
    report = json.loads(AIToolbox.execute('sales_report', args, location.id))
    snapshot = AIStockAssistant._get_sales_data(location.id)
    menu = AIStockAssistant._get_menu_engineering(3, location.id)
    velocity = AIStockAssistant._get_sales_velocity(3, location.id)

    assert listed['total_matching'] == 1
    assert [row['uuid'] for row in listed['orders']] == [str(opened_order.uuid)]
    assert listed_shifts['total_matching'] == 1
    assert [row['id'] for row in listed_shifts['shifts']] == [opened_shift.id]
    assert report['totals']['orders'] == 1
    assert report['totals']['paid_orders'] == 1
    assert report['totals']['paid_revenue_uzs'] == 100.0
    assert report['top_products'][0]['qty'] == 1
    assert snapshot['this_week']['count'] == 1
    assert snapshot['this_week']['total_revenue_uzs'] == 100.0
    assert snapshot['this_month']['count'] == 1
    assert menu['items'][0]['qty_sold'] == 1
    assert velocity['products'][0]['total_qty'] == 1

    # The legacy repository APIs receive inclusive bounds. They must recover
    # the same canonical window without changing their response shapes.
    window = resolve_reporting_window(date_from, date_to)
    inclusive_end = window.end_at - timedelta(microseconds=1)
    aggregate = OrderRepository.get_stats_aggregate(
        window.start_at, inclusive_end,
    )
    ranked = OrderItemRepository.get_top_products(
        window.start_at, inclusive_end,
    )

    assert aggregate['total'] == 1
    assert aggregate['paid'] == 1
    assert aggregate['total_revenue'] == Decimal('100.00')
    assert ranked[0]['total_qty'] == 1
    assert ranked[0]['total_revenue'] == Decimal('100.00')


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_order_detail_uses_net_total_and_stable_identity():
    user = _user()
    location = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    order = _order(
        user, subtotal='100', discount='10', total='90', order_number=17,
    )
    order.discount_percent = Decimal('10')
    order.save(update_fields=['discount_percent'])

    detail = json.loads(AIToolbox.execute(
        'get_order', {'order_uuid': str(order.uuid)}, location.id,
    ))

    assert detail['uuid'] == str(order.uuid)
    assert detail['order_number'] == 17
    assert detail['branch_id'] == 'branch-a'
    assert detail['total_amount_uzs'] == 90.0
    assert detail['amount_due_uzs'] == 90.0  # discount is not applied twice


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_legacy_display_id_never_silently_picks_one_of_duplicates():
    user = _user()
    location = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    _order(user, display_id=1, order_number=1)
    _order(user, display_id=1, order_number=2)

    result = json.loads(AIToolbox.execute(
        'get_order', {'display_id': 1}, location.id,
    ))

    assert 'ambiguous display_id' in result['error']


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_location_scopes_orders_and_selects_that_branch_live_register(monkeypatch):
    event_at = _freeze_at_open_business_time(monkeypatch)
    user = _user()
    a = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    StockLocation.objects.create(
        name='B', type=StockLocation.LocationType.KITCHEN, branch_id='branch-b',
    )
    CashRegister.objects.create(branch_id='branch-a', current_balance='123')
    CashRegister.objects.create(branch_id='branch-b', current_balance='999')
    own = _order(
        user, branch='branch-a', total='50',
        created_at=event_at, paid_at=event_at,
    )
    _order(
        user, branch='branch-b', total='700',
        created_at=event_at, paid_at=event_at,
    )

    overview = json.loads(AIToolbox.execute('get_overview', {}, a.id))
    unscoped = json.loads(AIToolbox.execute('get_overview', {}))
    listed = json.loads(AIToolbox.execute('list_orders', {}, a.id))
    snapshot = AIStockAssistant._get_sales_data(a.id)

    assert overview['cash_register_balance_uzs'] == 123.0
    assert overview['cash_register_branch_id'] == 'branch-a'
    assert unscoped['cash_register_balance_uzs'] is None
    assert overview['today_sales']['orders'] == 1
    assert overview['today_sales']['paid_revenue_uzs'] == 50.0
    assert [row['uuid'] for row in listed['orders']] == [str(own.uuid)]
    assert snapshot['today']['total_revenue_uzs'] == 50.0
    assert snapshot['cash_register_balance_uzs'] == 123.0


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_generic_customer_query_is_branch_scoped():
    location = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    Customer.objects.create(name='Own customer', branch_id='branch-a')
    Customer.objects.create(name='Other customer', branch_id='branch-b')

    result = json.loads(AIToolbox.execute(
        'query_db',
        {'model': 'customer', 'aggregate': {'n': 'count'}},
        location.id,
    ))

    assert result['matched'] == 1
    assert result['result']['n'] == 1


def test_generic_query_money_guidance_preserves_immutable_ledgers():
    from stock.services.ai_tools_service import _QUERYABLE_MODELS

    query_tool = next(
        tool for tool in AIToolbox.TOOLS if tool['name'] == 'query_db'
    )
    description = query_tool['description']

    assert 'NEVER exclude a paid sale because of status' in description
    assert 'Refunds are separate orderrefund events at refunded_at' in description
    assert 'not __date' in description
    assert 'paid_at__date' not in description
    assert {'orderpayment', 'orderrefund'} <= set(_QUERYABLE_MODELS)


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_generic_payment_and_refund_ledgers_are_branch_scoped():
    location = StockLocation.objects.create(
        name='Ledger A', type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-a',
    )
    user = _user('ledger-query@test.local')
    own = _order(user, branch='branch-a', total='20', paid_at=timezone.now())
    other = _order(user, branch='branch-b', total='90', paid_at=timezone.now())
    OrderPayment.objects.create(
        order=own, method='CASH', amount='20', branch_id='branch-a',
    )
    OrderPayment.objects.create(
        order=other, method='CASH', amount='90', branch_id='branch-b',
    )
    OrderRefund.objects.create(
        order=own, branch_id='branch-a', amount='20',
        cash_amount='20', refunded_at=timezone.now(),
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id=f'own-{uuid4().hex}',
    )
    OrderRefund.objects.create(
        order=other, branch_id='branch-b', amount='90',
        cash_amount='90', refunded_at=timezone.now(),
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id=f'other-{uuid4().hex}',
    )

    payments = json.loads(AIToolbox.execute(
        'query_db',
        {'model': 'orderpayment', 'aggregate': {'n': 'count'}},
        location.id,
    ))
    refunds = json.loads(AIToolbox.execute(
        'query_db',
        {'model': 'orderrefund', 'aggregate': {'n': 'count'}},
        location.id,
    ))

    assert payments['matched'] == payments['result']['n'] == 1
    assert refunds['matched'] == refunds['result']['n'] == 1


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_generic_query_refuses_raw_order_payment_money_sum():
    """The generic AI escape hatch must not bypass canonical tender math."""
    location = StockLocation.objects.create(
        name='Tender guard', type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-a',
    )
    user = _user('tender-guard@test.local')
    order = _order(user, branch='branch-a', total='100', paid_at=timezone.now())
    OrderPayment.objects.create(
        order=order, method='CASH', amount='200', branch_id='branch-a',
    )

    result = json.loads(AIToolbox.execute(
        'query_db',
        {'model': 'orderpayment', 'aggregate': {'money': 'sum:amount'}},
        location.id,
    ))

    assert 'raw OrderPayment money aggregation is disabled' in result['error']


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_order_detail_exposes_canonical_tender_not_raw_cash_sum():
    location = StockLocation.objects.create(
        name='Tender detail', type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-a',
    )
    user = _user('tender-detail@test.local')
    order = _order(user, branch='branch-a', total='100', paid_at=timezone.now())
    order.payment_method = Order.PaymentMethod.CASH
    order.save(update_fields=['payment_method'])
    # Two raw rows total 200, but the bill and drawer credit are only 100.
    OrderPayment.objects.create(
        order=order, method='CASH', amount='100', branch_id='branch-a',
    )
    OrderPayment.objects.create(
        order=order, method='CASH', amount='100', branch_id='branch-a',
    )

    result = json.loads(AIToolbox.execute(
        'get_order', {'order_id': order.id}, location.id,
    ))

    assert sum(row['amount_uzs'] for row in result['payments']) == 200.0
    assert result['canonical_tender']['cash_uzs'] == 100.0
    assert result['canonical_tender']['drawer_cash_uzs'] == 100.0
    assert result['canonical_tender']['unknown_uzs'] == 0.0
    assert 'must not be summed' in result['payment_evidence_note']


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_shift_volume_uses_created_at_but_revenue_uses_paid_at_and_branch():
    user = _user()
    location = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    now = timezone.now()
    shift = Shift.objects.create(
        user=user, branch_id='branch-a', status=Shift.Status.ENDED,
        start_time=now - timedelta(hours=1), end_time=now,
    )
    # Opened during this shift but paid after it: volume only.
    _order(
        user, branch='branch-a', total='100',
        created_at=now - timedelta(minutes=30), paid_at=now + timedelta(minutes=1),
    )
    # Opened before this shift but settled during it: drawer revenue only.
    paid_here = _order(
        user, branch='branch-a', total='200',
        created_at=now - timedelta(hours=2), paid_at=now - timedelta(minutes=20),
    )
    # Same cashier/time, different branch: neither metric may include it.
    _order(
        user, branch='branch-b', total='900',
        created_at=now - timedelta(minutes=10), paid_at=now - timedelta(minutes=5),
    )

    result = json.loads(AIToolbox.execute(
        'get_shift', {'shift_id': shift.id}, location.id,
    ))

    assert result['live_orders'] == 1
    assert result['live_paid_revenue_uzs'] == 200.0
    assert result['live_gross_uzs'] == 200.0
    assert all(row['uuid'] != str(paid_here.uuid) for row in result['orders'])


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_sales_report_buckets_pre_cutover_sale_and_allocates_net_revenue():
    user = _user()
    location = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    category = Category.objects.create(name='Food', branch_id='branch-a')
    product = Product.objects.create(
        name='Burger', category=category, price='100', branch_id='branch-a',
    )
    day = business_date() - timedelta(days=2)
    created = _local_at(day + timedelta(days=1), 1, 30)
    order = _order(
        user, subtotal='100', discount='10', total='90',
        created_at=created, paid_at=created,
    )
    order.payment_method = Order.PaymentMethod.MIXED
    order.save(update_fields=['payment_method'])
    OrderPayment.objects.create(
        order=order, method=Order.PaymentMethod.HUMO, amount='40',
        branch_id='branch-a',
    )
    # CASH rows store tendered cash and may include change. Canonical tender
    # reporting derives cash as net total minus non-cash (90 - 40 = 50).
    OrderPayment.objects.create(
        order=order, method=Order.PaymentMethod.CASH, amount='60',
        branch_id='branch-a',
    )
    OrderItem.objects.create(
        order=order, product=product, quantity=1, price='100', branch_id='branch-a',
    )

    report = json.loads(AIToolbox.execute(
        'sales_report', {'date': day.isoformat()}, location.id,
    ))

    assert report['totals']['paid_revenue_uzs'] == 90.0
    assert report['totals']['gross_uzs'] == 100.0
    assert report['totals']['total_discount_uzs'] == 10.0
    assert report['by_day'] == [{
        'date': day.isoformat(), 'orders': 1, 'revenue_uzs': 90.0,
        'refunds_uzs': 0.0, 'refunded_orders': 0,
    }]
    assert report['top_products'][0]['revenue_uzs'] == 90.0
    assert report['by_category'][0]['revenue_uzs'] == 90.0
    assert report['by_payment_method']['cash']['revenue_uzs'] == 50.0
    assert report['by_payment_method']['card']['revenue_uzs'] == 40.0
    assert report['by_payment_method']['unknown']['revenue_uzs'] == 0.0
    assert report['card_tender_detail']['HUMO'] == 40.0


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_created_day_gets_volume_while_paid_day_gets_money_and_product_sales(
    monkeypatch,
):
    _freeze_at_open_business_time(monkeypatch)
    user = _user()
    location = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    category = Category.objects.create(name='Food', branch_id='branch-a')
    product = Product.objects.create(
        name='Late-paid burger', category=category, price='100', branch_id='branch-a',
    )
    paid_day = business_date()
    created_day = paid_day - timedelta(days=1)
    order = _order(
        user, total='100',
        created_at=_local_at(created_day, 10),
        # Keep the settlement inside "today so far" even when the suite runs
        # before 10:00 local time.
        paid_at=timezone.now() - timedelta(minutes=1),
    )
    OrderItem.objects.create(
        order=order, product=product, quantity=1, price='100', branch_id='branch-a',
    )

    creation_report = json.loads(AIToolbox.execute(
        'sales_report', {'date': created_day.isoformat()}, location.id,
    ))
    payment_report = json.loads(AIToolbox.execute(
        'sales_report', {'date': paid_day.isoformat()}, location.id,
    ))
    snapshot = AIStockAssistant._get_sales_data(location.id)
    menu = AIStockAssistant._get_menu_engineering(1, location.id)
    velocity = AIStockAssistant._get_sales_velocity(1, location.id)

    assert creation_report['totals']['orders'] == 1
    assert creation_report['totals']['paid_revenue_uzs'] == 0.0
    assert creation_report['top_products'] == []
    assert creation_report['by_day'] == [{
        'date': created_day.isoformat(), 'orders': 1, 'revenue_uzs': 0.0,
        'refunds_uzs': 0.0, 'refunded_orders': 0,
    }]
    assert payment_report['totals']['orders'] == 0
    assert payment_report['totals']['paid_orders'] == 1
    assert payment_report['totals']['paid_revenue_uzs'] == 100.0
    assert payment_report['top_products'][0]['name'] == 'Late-paid burger'
    assert payment_report['by_day'] == [{
        'date': paid_day.isoformat(), 'orders': 0, 'revenue_uzs': 100.0,
        'refunds_uzs': 0.0, 'refunded_orders': 0,
    }]
    assert snapshot['today']['count'] == 0
    assert snapshot['today']['paid'] == 1
    assert snapshot['today']['total_revenue_uzs'] == 100.0
    assert snapshot['top_products_today'][0]['name'] == 'Late-paid burger'
    assert menu['items'][0]['name'] == 'Late-paid burger'
    assert velocity['products'][0]['name'] == 'Late-paid burger'


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_canceled_unpaid_order_is_not_reported_as_outstanding(monkeypatch):
    event_at = _freeze_at_open_business_time(monkeypatch)
    user = _user()
    location = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    _order(user, paid=False, status=Order.Status.CANCELED, created_at=event_at)
    _order(user, paid=False, status=Order.Status.READY, created_at=event_at)

    report = json.loads(AIToolbox.execute(
        'sales_report', {'date': business_date().isoformat()}, location.id,
    ))
    overview = json.loads(AIToolbox.execute('get_overview', {}, location.id))
    snapshot = AIStockAssistant._get_sales_data(location.id)

    assert report['totals']['orders'] == 2
    assert report['totals']['unpaid_orders'] == 1
    assert overview['today_sales']['unpaid_orders'] == 1
    assert snapshot['today']['unpaid'] == 1


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_menu_and_velocity_only_use_live_paid_branch_sales_at_net_value(
    monkeypatch,
):
    _freeze_at_open_business_time(monkeypatch)
    user = _user()
    location = StockLocation.objects.create(
        name='A', type=StockLocation.LocationType.KITCHEN, branch_id='branch-a',
    )
    category = Category.objects.create(name='Food', branch_id='branch-a')
    product = Product.objects.create(
        name='Valid burger', category=category, price='100', branch_id='branch-a',
    )
    valid = _order(
        user, subtotal='100', discount='10', total='90', paid_at=timezone.now(),
    )
    OrderItem.objects.create(
        order=valid, product=product, quantity=1, price='100', branch_id='branch-a',
    )

    excluded = [
        _order(user, total='300', paid=False),
        _order(user, total='400', status=Order.Status.CANCELED, paid_at=timezone.now()),
        _order(user, branch='branch-b', total='600', paid_at=timezone.now()),
    ]
    for i, order in enumerate(excluded):
        other = Product.objects.create(
            name=f'Excluded {i}', category=category, price=order.total_amount,
            branch_id=order.branch_id,
        )
        OrderItem.objects.create(
            order=order, product=other, quantity=1, price=order.total_amount,
            branch_id=order.branch_id,
        )
    # A paid cancellation remains a gross sale at paid_at plus a separate
    # negative event at refunded_at. The data migration creates this ledger row
    # for legacy cancellations; model the valid invariant here instead of
    # expecting analytics to erase a paid sale merely from its current status.
    cancelled = excluded[1]
    OrderRefund.objects.create(
        order=cancelled,
        amount=cancelled.total_amount,
        cash_amount=cancelled.total_amount,
        drawer_cash_amount=cancelled.total_amount,
        card_amount=0,
        payme_amount=0,
        unknown_amount=0,
        refunded_at=timezone.now(),
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=f'order-cancel:{cancelled.uuid}',
        branch_id=cancelled.branch_id,
    )
    deleted_order = _order(user, total='500', paid_at=timezone.now())
    deleted_product = Product.objects.create(
        name='Deleted line', category=category, price='500', branch_id='branch-a',
    )
    OrderItem.objects.create(
        order=deleted_order, product=deleted_product, quantity=1, price='500',
        is_deleted=True, branch_id='branch-a',
    )

    menu = AIStockAssistant._get_menu_engineering(30, location.id)
    velocity = AIStockAssistant._get_sales_velocity(30, location.id)

    assert [(row['name'], row['qty_sold'], row['revenue_uzs']) for row in menu['items']] == [
        ('Valid burger', 1, 90.0),
    ]
    assert [(row['name'], row['total_qty'], row['total_revenue_uzs'])
            for row in velocity['products']] == [('Valid burger', 1, 90.0)]


@override_settings(
    DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud',
    CLOUD_DEFAULT_TARGET_BRANCH_ID='branch-a',
)
def test_recipe_snapshot_and_menu_share_canonical_units_yield_waste_and_scope(
    monkeypatch,
):
    _freeze_at_open_business_time(monkeypatch)
    user = _user()
    location = StockLocation.objects.create(
        name='Kitchen A', type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-a',
    )
    other_location = StockLocation.objects.create(
        name='Kitchen B', type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-b',
    )
    gram, kilogram, piece = _stock_units()
    flour = StockItem.objects.create(
        name='Flour', sku='FLOUR', base_unit=gram,
        item_type=StockItem.ItemType.RAW, avg_cost_price='2',
    )
    garnish = StockItem.objects.create(
        name='Optional garnish', sku='GARNISH', base_unit=gram,
        item_type=StockItem.ItemType.RAW, avg_cost_price='0',
    )
    obsolete = StockItem.objects.create(
        name='Obsolete ingredient', sku='OBSOLETE', base_unit=gram,
        item_type=StockItem.ItemType.RAW, avg_cost_price='999',
        is_active=False, is_deleted=True,
    )
    output = StockItem.objects.create(
        name='Dough portion', sku='DOUGH', base_unit=piece,
        item_type=StockItem.ItemType.FINISHED, is_producible=True,
    )
    StockLevel.objects.create(
        stock_item=flour, location=location, quantity='1200',
        reserved_quantity='100',
        branch_id='branch-a',
    )
    # A huge amount at another branch must not satisfy this kitchen's recipe.
    StockLevel.objects.create(
        stock_item=garnish, location=other_location, quantity='999999',
        branch_id='branch-b',
    )
    recipe = Recipe.objects.create(
        name='Shared dough', code='SHARED-DOUGH', output_item=output,
        output_quantity='10', output_unit=piece,
        recipe_type=Recipe.RecipeType.PRODUCTION, yield_percentage='80',
        production_location=None,
    )
    RecipeIngredient.objects.create(
        recipe=recipe, stock_item=flour, quantity='1', unit=kilogram,
        waste_percentage='10', sort_order=1,
    )
    RecipeIngredient.objects.create(
        recipe=recipe, stock_item=garnish, quantity='1', unit=gram,
        is_optional=True, sort_order=2,
    )
    RecipeIngredient.objects.create(
        recipe=recipe, stock_item=flour, quantity='100', unit=kilogram,
        is_deleted=True, sort_order=3,
    )
    RecipeIngredient.objects.create(
        recipe=recipe, stock_item=obsolete, quantity='100', unit=kilogram,
        sort_order=4,
    )
    Recipe.objects.create(
        name='Other kitchen recipe', code='OTHER-DOUGH', output_item=output,
        output_quantity='1', output_unit=piece,
        recipe_type=Recipe.RecipeType.PRODUCTION,
        production_location=other_location,
    )

    supplier = Supplier.objects.create(name='Supplier')
    PurchaseOrder.objects.create(
        order_number='PO-A', supplier=supplier, delivery_location=location,
        status=PurchaseOrder.Status.SENT, order_date=timezone.localdate(),
        created_by=user,
    )
    PurchaseOrder.objects.create(
        order_number='PO-B', supplier=supplier, delivery_location=other_location,
        status=PurchaseOrder.Status.SENT, order_date=timezone.localdate(),
        created_by=user,
    )

    category = Category.objects.create(name='Food', branch_id='branch-a')
    product = Product.objects.create(
        name='Dough sale', category=category, price='1000', branch_id='branch-a',
    )
    # The link is global catalog metadata. Its sync provenance is deliberately
    # another branch and must not hide the only COGS definition.
    ProductStockLink.objects.create(
        product=product, link_type=ProductStockLink.LinkType.RECIPE,
        recipe=recipe, quantity_per_sale='1', unit=piece,
        branch_id='branch-b',
    )
    order = _order(user, total='1000', paid_at=timezone.now())
    OrderItem.objects.create(
        order=order, product=product, quantity=1, price='1000',
        branch_id='branch-a',
    )

    snapshot = AIStockAssistant._get_all_stock_data(location.id)
    menu = AIStockAssistant._get_menu_engineering(1, location.id)
    deductions = ProductStockLinkService.get_deduction_items(product.id, 1)
    availability, status = RecipeService.check_availability(
        recipe.id, location_id=location.id,
    )

    assert [po['number'] for po in snapshot['pending_purchase_orders']] == ['PO-A']
    assert [row['name'] for row in snapshot['recipes']] == ['Shared dough']
    recipe_row = snapshot['recipes'][0]
    assert recipe_row['effective_output_qty'] == 8.0
    assert recipe_row['total_cost_uzs'] == 2200.0
    assert recipe_row['cost_per_unit_uzs'] == 275.0
    assert recipe_row['can_produce'] is True
    assert [row['item'] for row in recipe_row['ingredients']] == [
        'Flour', 'Optional garnish',
    ]
    flour_row, optional_row = recipe_row['ingredients']
    assert flour_row['base_qty'] == 1100.0
    assert flour_row['available'] == 1100.0
    assert flour_row['enough'] is True
    assert optional_row['optional'] is True
    assert optional_row['available'] == 0.0
    assert optional_row['enough'] is False
    assert menu['items'][0]['cost_known'] is True
    assert menu['items'][0]['ingredient_cost_uzs'] == 275.0
    assert status == 200
    assert [row['stock_item_name'] for row in availability['data']['ingredients']] == [
        'Flour', 'Optional garnish',
    ]
    assert availability['data']['ingredients'][0]['required_base_quantity'] == '1100.0000'
    assert availability['data']['ingredients'][0]['available_stock'] == '1100.0000'
    assert availability['data']['ingredients'][1]['is_available'] is True
    assert deductions == [{
        'stock_item_id': flour.id,
        'quantity': Decimal('0.1375'),
        'unit_id': kilogram.id,
    }, {
        'stock_item_id': garnish.id,
        'quantity': Decimal('0.1250'),
        'unit_id': gram.id,
    }]


@override_settings(
    DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud',
    CLOUD_DEFAULT_TARGET_BRANCH_ID='branch-a',
)
def test_direct_and_component_cogs_convert_units_and_ignore_deleted_components():
    gram, kilogram, _piece = _stock_units()
    ingredient = StockItem.objects.create(
        name='Ingredient', sku='ING', base_unit=gram,
        item_type=StockItem.ItemType.RAW, avg_cost_price='2',
    )
    category = Category.objects.create(name='Food')
    direct_product = Product.objects.create(
        name='Direct', category=category, price='2000',
    )
    direct = ProductStockLink.objects.create(
        product=direct_product, link_type=ProductStockLink.LinkType.DIRECT_ITEM,
        stock_item=ingredient, quantity_per_sale='0.5', unit=kilogram,
    )
    component_product = Product.objects.create(
        name='Component', category=category, price='2000',
    )
    component_link = ProductStockLink.objects.create(
        product=component_product,
        link_type=ProductStockLink.LinkType.COMPONENT_BASED,
    )
    ProductComponentStock.objects.create(
        product_stock_link=component_link, component_name='Live',
        stock_item=ingredient, quantity='0.25', unit=kilogram,
    )
    ProductComponentStock.objects.create(
        product_stock_link=component_link, component_name='Deleted',
        stock_item=ingredient, quantity='10', unit=kilogram, is_deleted=True,
    )

    assert ProductStockLinkService.calculate_unit_cost(direct) == Decimal('1000')
    assert ProductStockLinkService.calculate_unit_cost(component_link) == Decimal('500')


@override_settings(DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud')
def test_uncosted_menu_item_is_not_reported_as_zero_cost_profit(monkeypatch):
    _freeze_at_open_business_time(monkeypatch)
    user = _user()
    location = StockLocation.objects.create(
        name='Kitchen A', type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-a',
    )
    category = Category.objects.create(name='Food', branch_id='branch-a')
    product = Product.objects.create(
        name='No stock link', category=category, price='100', branch_id='branch-a',
    )
    order = _order(user, total='100', paid_at=timezone.now())
    OrderItem.objects.create(
        order=order, product=product, quantity=1, price='100', branch_id='branch-a',
    )

    menu = AIStockAssistant._get_menu_engineering(1, location.id)
    profitability = AIStockAssistant._get_profitability_analysis(1, location.id)

    row = menu['items'][0]
    assert row['cost_known'] is False
    assert row['ingredient_cost_uzs'] is None
    assert row['profit_uzs'] is None
    assert row['category_me'] == 'Uncosted'
    assert menu['summary']['uncosted'] == 1
    assert profitability['summary']['gross_profit_uzs'] == 0
    assert profitability['summary']['uncosted_revenue_uzs'] == 100.0


@override_settings(
    DEPLOYMENT_MODE='cloud', BRANCH_ID='cloud',
    CLOUD_DEFAULT_TARGET_BRANCH_ID='branch-a',
)
def test_consumption_is_descending_abc_keeps_dominant_item_in_a_and_forecast_uses_available():
    user = _user()
    location = StockLocation.objects.create(
        name='Kitchen A', type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-a',
    )
    gram, _kilogram, _piece = _stock_units()
    dominant = StockItem.objects.create(
        name='Dominant', sku='DOM', base_unit=gram,
        item_type=StockItem.ItemType.RAW, avg_cost_price='1',
    )
    minor = StockItem.objects.create(
        name='Minor', sku='MIN', base_unit=gram,
        item_type=StockItem.ItemType.RAW, avg_cost_price='1',
    )
    StockLevel.objects.create(
        stock_item=dominant, location=location, quantity='100',
        reserved_quantity='40', branch_id='branch-a',
    )
    StockLevel.objects.create(
        stock_item=minor, location=location, quantity='100',
        branch_id='branch-a',
    )
    for number, item, quantity in (
        ('TX-DOM', dominant, Decimal('-90')),
        ('TX-MIN', minor, Decimal('-10')),
    ):
        StockTransaction.objects.create(
            transaction_number=number, stock_item=item, location=location,
            movement_type=StockTransaction.MovementType.SALE_OUT,
            quantity=quantity, unit=gram, base_quantity=quantity,
            quantity_before='100', quantity_after=Decimal('100') + quantity,
            unit_cost='1', total_cost=quantity, user=user,
            branch_id='branch-a',
        )

    snapshot = AIStockAssistant._get_all_stock_data(location.id)
    abc = AIStockAssistant._get_abc_analysis(30, location.id)

    assert [row['item'] for row in snapshot['consumption_30_days'][:2]] == [
        'Dominant', 'Minor',
    ]
    dominant_forecast = next(
        row for row in snapshot['forecasts'] if row['item'] == 'Dominant'
    )
    assert dominant_forecast['available_stock'] == 60.0
    assert dominant_forecast['days_until_stockout'] == 20
    assert [(row['name'], row['abc_class']) for row in abc['items']] == [
        ('Dominant', 'A'), ('Minor', 'B'),
    ]
