"""Independent PostgreSQL transactions, never mocked locks."""

import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections

from base.models import AuditLog, IdempotencyKey, User
from stock.models import (
    PurchaseReceiving,
    StockItem,
    StockLevel,
    StockTransaction,
    SupplierStockItem,
    SupplierTransaction,
)
from stock.services.level_service import StockLevelService
from stock.services.purchase_invoices import json_numbers
from stock.services.purchase_invoices.commands import DirectPurchaseInvoiceService

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="Requires PostgreSQL independent row locks",
    ),
]


def race(operation):
    barrier = Barrier(2)

    def worker(index):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', '10s', false), pg_backend_pid()"
                )
                pid = cursor.fetchone()[1]
            barrier.wait(timeout=10)
            return pid, operation(index)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = [workers.submit(worker, index) for index in range(2)]
        results = [job.result(timeout=25) for job in jobs]
    assert len({pid for pid, _ in results}) == 2
    return [result for _, result in results]


def test_same_key_concurrent_posts_return_exact_response_once(invoice_data):
    data = invoice_data
    results = race(
        lambda _: DirectPurchaseInvoiceService.receive(
            actor=User.objects.get(pk=data.warehouse.id),
            payload=copy.deepcopy(data.payload),
            idempotency_key="same-key",
        )
    )
    assert [status for _, status in results] == [201, 201], results
    assert json_numbers.dumps(results[0][0]) == json_numbers.dumps(results[1][0])
    assert (
        PurchaseReceiving.objects.count()
        == StockTransaction.objects.count()
        == IdempotencyKey.objects.count()
        == 1
    )
    assert SupplierTransaction.objects.filter(type="PURCHASE").count() == 1
    assert AuditLog.objects.filter(action="SUPPLIER_INVOICE_RECEIVE").count() == 1
    data.level.refresh_from_db()
    data.supplier.refresh_from_db()
    assert data.level.quantity == 15 and data.supplier.current_balance == 700000


def test_concurrent_business_duplicate_with_different_actors_and_keys(invoice_data):
    data = invoice_data
    results = race(
        lambda index: DirectPurchaseInvoiceService.receive(
            actor=User.objects.get(
                pk=(data.warehouse.id if index == 0 else data.manager.id)
            ),
            payload=copy.deepcopy(data.payload),
            idempotency_key=f"actor-{index}",
        )
    )
    assert sorted(status for _, status in results) == [201, 409], results
    assert (
        next(body for body, status in results if status == 409)["code"]
        == "DUPLICATE_SUPPLIER_INVOICE"
    )
    assert PurchaseReceiving.objects.count() == StockTransaction.objects.count() == 1
    assert SupplierTransaction.objects.filter(type="PURCHASE").count() == 1


def test_opposite_line_order_uses_sorted_locks_across_suppliers(invoice_data):
    from stock.models import Supplier

    data = invoice_data
    second = StockItem.objects.create(
        name="Second", base_unit=data.kg, item_type="RAW", branch_id="branch1"
    )
    second_link = SupplierStockItem.objects.create(
        supplier=data.supplier,
        stock_item=second,
        unit=data.kg,
        price=100000,
        price_is_known=True,
        branch_id="branch1",
    )
    supplier = Supplier.objects.create(name="Another supplier", branch_id="branch1")
    other_links = [
        SupplierStockItem.objects.create(
            supplier=supplier,
            stock_item=item,
            unit=data.kg,
            price=100000,
            price_is_known=True,
            branch_id="branch1",
        )
        for item in (data.item, second)
    ]
    payloads = [copy.deepcopy(data.payload), copy.deepcopy(data.payload)]
    payloads[0]["lines"].append(
        dict(payloads[0]["lines"][0], supplier_item_id=second_link.id)
    )
    payloads[1]["supplier_id"] = supplier.id
    payloads[1]["lines"] = [
        dict(payloads[0]["lines"][0], supplier_item_id=link.id)
        for link in reversed(other_links)
    ]
    for payload in payloads:
        payload["declared_total_uzs"] = 1000000
    results = race(
        lambda index: DirectPurchaseInvoiceService.receive(
            actor=User.objects.get(pk=data.warehouse.id),
            payload=payloads[index],
            idempotency_key=f"sorted-{index}",
        )
    )
    assert [status for _, status in results] == [201, 201], results
    assert (
        PurchaseReceiving.objects.count() == 2 and StockTransaction.objects.count() == 4
    )
    data.level.refresh_from_db()
    data.item.refresh_from_db()
    second.refresh_from_db()
    assert data.level.quantity == 20 and data.item.avg_cost_price == 90000
    assert (
        StockLevel.objects.get(stock_item=second).quantity == 10
        and second.avg_cost_price == 100000
    )


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_full_reversals_compensate_once(invoice_data, same_key):
    data = invoice_data
    body, status = DirectPurchaseInvoiceService.receive(
        actor=data.warehouse, payload=data.payload, idempotency_key="post"
    )
    assert status == 201
    results = race(
        lambda index: DirectPurchaseInvoiceService.reverse(
            actor=User.objects.get(pk=data.manager.id),
            invoice_id=body["data"]["invoice"]["id"],
            payload={"reason": "Correction"},
            idempotency_key="reverse" if same_key else f"reverse-{index}",
        )
    )
    assert sorted(status for _, status in results) == (
        [200, 200] if same_key else [200, 409]
    ), results
    if same_key:
        assert json_numbers.dumps(results[0][0]) == json_numbers.dumps(results[1][0])
    assert (
        StockTransaction.objects.filter(movement_type="RETURN_TO_SUPPLIER").count() == 1
    )
    assert SupplierTransaction.objects.filter(type="RETURN").count() == 1
    data.level.refresh_from_db()
    data.supplier.refresh_from_db()
    assert data.level.quantity == 10 and data.supplier.current_balance == 200000


def test_reversal_and_sale_serialize_on_available_stock(invoice_data):
    data = invoice_data
    body, status = DirectPurchaseInvoiceService.receive(
        actor=data.warehouse, payload=data.payload, idempotency_key="post"
    )
    assert status == 201

    def operation(index):
        if index:
            return StockLevelService.adjust(
                data.item.id, data.location.id, 1, "SALE_OUT", data.manager.id
            )
        return DirectPurchaseInvoiceService.reverse(
            actor=User.objects.get(pk=data.manager.id),
            invoice_id=body["data"]["invoice"]["id"],
            payload={"reason": "Correction"},
            idempotency_key="reverse",
        )

    results = race(operation)
    assert results[1][1] == 200 and results[0][1] in (200, 409), results
    data.level.refresh_from_db()
    assert data.level.quantity == (9 if results[0][1] == 200 else 14)
    if results[0][1] == 409:
        assert results[0][0]["code"] == "INVOICE_REVERSAL_STOCK_CONSUMED"
