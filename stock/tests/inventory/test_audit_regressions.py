"""Accounting regressions found in the September 23 source audit."""
from datetime import timedelta

import pytest
from django.utils import timezone

from stock.models import (
    StockBatch, StockLevel, StockTransfer,
)
from stock.services.batch_service import StockBatchService
from stock.services.order_service import OrderStockService
from stock.services.production_service import ProductionOrderService
from stock.services.transfer_service import StockTransferItemService, StockTransferService

pytestmark = pytest.mark.django_db


def make_transfer(c, quantity=10, unit=None, batch=None):
    result, status = StockTransferService.create(
        c.source.pk, c.dest.pk, c.actor.pk,
        items=[{'stock_item_id': c.item.pk, 'quantity': quantity,
                'unit_id': (unit or c.gram).pk, 'batch_id': batch.pk if batch else None}],
    )
    assert status == 200, result
    return StockTransfer.objects.get(pk=result['data']['id'])


def ship(c, transfer):
    for operation in (StockTransferService.approve, StockTransferService.ship):
        result, status = operation(transfer.pk, c.actor.pk)
        assert status == 200, result


def make_batch(c, **kwargs):
    return StockBatch.objects.create(
        stock_item=c.item, location=c.source, branch_id='branch1',
        batch_number='AUDIT-BATCH', initial_quantity=10000,
        current_quantity=10000, unit_cost=2, **kwargs,
    )


def test_cancel_returns_only_remaining_order_stock(catalog, order_factory):
    c = catalog
    order = order_factory()
    line = order.items.first()
    assert OrderStockService.deduct_for_order(
        order.pk, [{'product_id': c.product.pk, 'order_item_id': line.pk,
                    'quantity': 10}], c.source.pk, c.actor.pk,
    )[1] == 200
    assert OrderStockService.adjust_for_item_change(
        order.pk, c.product.pk, -4, c.source.pk, c.actor.pk, line.pk,
    )[1] == 200
    assert OrderStockService.reverse_deduction(order.pk, c.actor.pk)[1] == 200
    assert OrderStockService.reverse_deduction(order.pk, c.actor.pk)[1] == 200
    c.level.refresh_from_db()
    assert c.level.quantity == 10000


def test_transfer_honors_json_receipt_keys(catalog):
    c = catalog
    transfer = make_transfer(c)
    ship(c, transfer)
    line = transfer.items.get()
    result, status = StockTransferService.receive(
        transfer.pk, c.actor.pk, {str(line.pk): '2'},
    )
    assert status == 200, result
    assert StockLevel.objects.get(stock_item=c.item, location=c.dest).quantity == 2
    line.refresh_from_db()
    assert line.received_qty == 2


def test_transfer_uses_base_units_for_both_locations(catalog):
    c = catalog
    transfer = make_transfer(c, quantity=1, unit=c.kilo)
    ship(c, transfer)
    assert StockTransferService.receive(transfer.pk, c.actor.pk)[1] == 200
    c.level.refresh_from_db()
    assert c.level.quantity == 9000
    assert StockLevel.objects.get(stock_item=c.item, location=c.dest).quantity == 1000


def test_transfer_cancel_restores_batch(catalog):
    c = catalog
    batch = make_batch(c)
    transfer = make_transfer(c, quantity=10, batch=batch)
    ship(c, transfer)
    assert StockTransferService.cancel(transfer.pk)[1] == 200
    batch.refresh_from_db()
    c.level.refresh_from_db()
    assert batch.current_quantity == c.level.quantity == 10000


def test_invalid_second_transfer_item_leaves_no_document(catalog):
    c = catalog
    result, status = StockTransferService.create(
        c.source.pk, c.dest.pk, c.actor.pk,
        items=[{'stock_item_id': c.item.pk, 'quantity': 1},
               {'stock_item_id': 999999, 'quantity': 1}],
    )
    assert status >= 400, result
    assert not StockTransfer.objects.exists()


@pytest.mark.parametrize('quantity', ['-1', '0', 'NaN', 'Infinity', 'no number'])
def test_transfer_rejects_invalid_quantities(catalog, quantity):
    c = catalog
    result, status = StockTransferService.create(
        c.source.pk, c.dest.pk, c.actor.pk,
        items=[{'stock_item_id': c.item.pk, 'quantity': quantity}],
    )
    assert status == 422, result
    assert not StockTransfer.objects.exists()


def test_removed_transfer_line_cannot_be_requested(catalog):
    c = catalog
    transfer = make_transfer(c)
    assert StockTransferItemService.remove_item(transfer.items.get().pk)[1] == 200
    assert StockTransferService.request(transfer.pk)[1] >= 400
    assert StockTransferService.approve(transfer.pk, c.actor.pk)[1] >= 400


def test_batch_failure_rolls_back_batch_debit(catalog):
    c = catalog
    batch = make_batch(c)
    c.level.quantity = 1
    c.level.save(update_fields=['quantity'])
    result, status = StockBatchService.consume(batch.pk, 5, 'SALE_OUT', c.actor.pk)
    assert status >= 400, result
    batch.refresh_from_db()
    assert batch.current_quantity == 10000


@pytest.mark.parametrize('status,expired', [('QUARANTINE', False), ('AVAILABLE', True)])
def test_unusable_batch_cannot_be_consumed(catalog, status, expired):
    c = catalog
    batch = make_batch(c, status=status,
                       expiry_date=timezone.localdate() - timedelta(days=1) if expired else None)
    result, code = StockBatchService.consume(batch.pk, 1, 'SALE_OUT', c.actor.pk)
    assert code >= 400, result
    batch.refresh_from_db()
    assert batch.current_quantity == 10000


def test_production_reserves_and_consumes_base_units(catalog):
    c = catalog
    result, status = ProductionOrderService.create(c.recipe.pk, c.actor.pk)
    assert status == 200, result
    pk = result['data']['id']
    result, status = ProductionOrderService.plan(pk)
    assert status == 200, result
    c.level.refresh_from_db()
    assert c.level.reserved_quantity == 1000
    assert ProductionOrderService.start(pk, c.actor.pk)[1] == 200
    result, status = ProductionOrderService.complete(pk, 1, c.actor.pk)
    assert status == 200, result
    c.level.refresh_from_db()
    assert c.level.quantity == 9000
    assert c.level.reserved_quantity == 0
    assert StockLevel.objects.get(stock_item=c.output, location=c.dest).quantity == 1000


@pytest.mark.parametrize('held', [False, True])
def test_cancel_production_releases_all_allocations(catalog, held):
    c = catalog
    result, status = ProductionOrderService.create(c.recipe.pk, c.actor.pk, auto_allocate=True)
    assert status == 200, result
    pk = result['data']['id']
    if held:
        assert ProductionOrderService.plan(pk)[1] == 200
        assert ProductionOrderService.hold(pk)[1] == 200
    assert ProductionOrderService.cancel(pk)[1] == 200
    c.level.refresh_from_db()
    assert c.level.reserved_quantity == 0


def test_deleted_ingredient_is_not_copied_into_production(catalog):
    c = catalog
    c.ingredient.delete()
    result, status = ProductionOrderService.create(c.recipe.pk, c.actor.pk)
    assert status == 200, result
    from stock.models import ProductionOrderIngredient
    assert not ProductionOrderIngredient.objects.filter(
        production_order_id=result['data']['id'], is_deleted=False,
    ).exists()


@pytest.mark.parametrize('operation', ['start', 'complete', 'skip'])
def test_production_steps_cannot_change_after_cancellation(catalog, operation):
    from stock.models import ProductionOrderStep, RecipeStep
    from stock.services.production_service import ProductionOrderStepService

    c = catalog
    RecipeStep.objects.create(recipe=c.recipe, step_number=1, title='Mix')
    result, status = ProductionOrderService.create(c.recipe.pk, c.actor.pk)
    assert status == 200, result
    pk = result['data']['id']
    assert ProductionOrderService.plan(pk)[1] == 200
    assert ProductionOrderService.start(pk, c.actor.pk)[1] == 200
    assert ProductionOrderService.cancel(pk)[1] == 200
    step = ProductionOrderStep.objects.get(production_order_id=pk)
    kwargs = {'completed_by_id': c.actor.pk} if operation == 'complete' else {}
    result, status = getattr(ProductionOrderStepService, operation)(step.pk, **kwargs)
    assert status == 400, result
    step.refresh_from_db()
    assert step.status == 'PENDING'
