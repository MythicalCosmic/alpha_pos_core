from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections

from stock.models import StockLevel, StockTransaction
from stock.services.production_service import ProductionOrderService
from stock.services.transfer_service import StockTransferService
from .test_audit_regressions import make_transfer, ship

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != 'postgresql', reason='Requires PostgreSQL row locks'),
]


def race(operation):
    barrier = Barrier(2)

    def worker():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return operation()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        return [future.result(timeout=30) for future in futures]


def test_duplicate_ship_moves_stock_once(catalog):
    c = catalog
    transfer = make_transfer(c)
    assert StockTransferService.approve(transfer.pk, c.actor.pk)[1] == 200
    results = race(lambda: StockTransferService.ship(transfer.pk, c.actor.pk))
    assert sorted(status for _, status in results) == [200, 400], results
    c.level.refresh_from_db()
    assert c.level.quantity == 9990
    assert StockTransaction.objects.filter(transfer=transfer, movement_type='TRANSFER_OUT').count() == 1


def test_duplicate_receive_moves_stock_once(catalog):
    c = catalog
    transfer = make_transfer(c)
    ship(c, transfer)
    results = race(lambda: StockTransferService.receive(transfer.pk, c.actor.pk))
    assert sorted(status for _, status in results) == [200, 400], results
    assert StockLevel.objects.get(stock_item=c.item, location=c.dest).quantity == 10
    assert StockTransaction.objects.filter(transfer=transfer, movement_type='TRANSFER_IN').count() == 1


def test_duplicate_production_completion_produces_once(catalog):
    c = catalog
    created, status = ProductionOrderService.create(c.recipe.pk, c.actor.pk)
    assert status == 200, created
    pk = created['data']['id']
    assert ProductionOrderService.plan(pk)[1] == 200
    assert ProductionOrderService.start(pk, c.actor.pk)[1] == 200
    results = race(lambda: ProductionOrderService.complete(pk, 1, c.actor.pk))
    assert sorted(status for _, status in results) == [200, 400], results
    c.level.refresh_from_db()
    assert c.level.quantity == 9000
    assert c.level.reserved_quantity == 0
    assert StockLevel.objects.get(stock_item=c.output, location=c.dest).quantity == 1000
