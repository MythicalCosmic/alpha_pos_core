from decimal import Decimal

import pytest
from django.apps import apps

from base.models import Order
from stock.models import StockTransaction
from stock.services.order_service import OrderStockService
from stock.services.product_link_service import ProductStockLinkService

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('timing,instant', [
    ('CREATED', False), ('PREPARING', False), ('PREPARING', True),
    ('READY', False), ('READY', True),
])
def test_actual_create_and_ready_timing_deducts_once(inventory_catalog, regular_user, timing, instant):
    if not apps.is_installed('customers'):
        pytest.skip('Local order adapter test')
    from customers.services.order_service import CustomerOrderService
    c = inventory_catalog
    c.level.quantity = Decimal('1000'); c.level.save(update_fields=['quantity'])
    c.config.deduct_on_order_status = timing
    c.config.reserve_on_order_create = False
    c.config.default_location = c.location
    c.config.save()
    c.product.is_instant = instant; c.product.save(update_fields=['is_instant'])
    result, status = CustomerOrderService.create_order(
        regular_user.pk, [{'product_id': c.product.pk, 'quantity': 1}], order_type='PICKUP')
    assert status == 201, result
    order = Order.objects.get(pk=result['data']['order_id'])
    c.level.refresh_from_db()
    assert c.level.quantity == (1000 if timing == 'READY' and not instant else 900)
    assert CustomerOrderService.mark_order_ready(order.pk)[1] == 200
    assert CustomerOrderService.mark_order_ready(order.pk)[1] == 200
    c.level.refresh_from_db()
    assert c.level.quantity == 900
    assert StockTransaction.objects.filter(order_id=order.pk, movement_type='SALE_OUT').count() == 1
    first = OrderStockService.reverse_deduction(order.pk, regular_user.pk)
    second = OrderStockService.reverse_deduction(order.pk, regular_user.pk)
    assert first[1] == second[1] == 200
    c.level.refresh_from_db(); assert c.level.quantity == 1000


def test_product_timing_reports_global_and_rejects_ignored_override(inventory_catalog):
    c = inventory_catalog
    c.config.deduct_on_order_status = 'READY'; c.config.save()
    result, _ = ProductStockLinkService.get_by_product(c.product.pk)
    link = result['data']['link']
    assert link['deduct_on_status'] == 'READY' and link['deduction_timing_scope'] == 'GLOBAL'
    assert ProductStockLinkService.update(link['id'], deduct_on_status='PREPARING')[1] == 422
