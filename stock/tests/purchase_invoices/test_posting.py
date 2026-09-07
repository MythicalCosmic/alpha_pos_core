import copy
from decimal import Decimal
from unittest.mock import patch

import pytest

from stock.models import (
    PurchaseReceiving,
    StockItem,
    StockItemUnit,
    StockLevel,
    StockTransaction,
    StockUnit,
    SupplierStockItem,
    SupplierTransaction,
)
from stock.services.purchase_invoices import json_numbers
from stock.services.purchase_invoices.validation import UNKNOWN_PRICE_MARKER
from stock.tests.test_warehouse_receiving import _client

from .helpers import BASE, ledger_state, post

pytestmark = pytest.mark.django_db


def test_example_exact_replay_and_no_cash_mutation(invoice_data, record_property):
    data = invoice_data
    before = ledger_state(data)
    response = post(data)
    assert response.status_code == 201, response.content
    invoice = json_numbers.loads(response.content)["data"]["invoice"]
    assert invoice["total_uzs"] == 500000
    assert invoice["supplier_balance_before_uzs"] == 200000
    assert invoice["supplier_balance_after_uzs"] == 700000
    assert invoice["lines"][0]["new_average_cost_uzs"] == Decimal("86666.6667")
    assert invoice["lines"][0]["stock_quantity_before"] == 10
    assert invoice["lines"][0]["stock_quantity_after"] == 15
    assert invoice["posted_at"].endswith("+05:00")
    assert invoice["allowed_actions"] == []
    after = ledger_state(data)
    assert after["manual"] == before["manual"]
    assert after["price"] == after["last"] == 100000
    assert after["quantity"] == 15
    assert after["stock_transactions"] == before["stock_transactions"] + 1
    assert after["supplier_transactions"] == before["supplier_transactions"] + 1
    assert (
        after["expenses"] == before["expenses"]
        and after["treasury"] == before["treasury"]
    )
    replay = post(data)
    assert (
        replay.status_code == response.status_code
        and replay.content == response.content
    )
    assert ledger_state(data) == after
    denied = data.client.post(
        f"/api/admins/stock/suppliers/{data.supplier.id}/pay/",
        '{"amount":1000}',
        content_type="application/json",
    )
    assert denied.status_code == 403
    record_property("example_request", json_numbers.dumps(data.payload))
    record_property("posted_response", response.content.decode())
    record_property("replay_response", replay.content.decode())
    record_property("forbidden_payment_response", denied.content.decode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", 0),
        ("quantity", -1),
        ("quantity", True),
        ("quantity", ""),
        ("quantity", "1e2"),
        ("quantity", Decimal(".0001")),
        ("quantity", Decimal("999999999999999")),
        ("unit_price_uzs", 0),
        ("unit_price_uzs", -1),
        ("unit_price_uzs", True),
        ("unit_price_uzs", ""),
        ("unit_price_uzs", Decimal("1.5")),
        ("unit_price_uzs", "NaN"),
        ("unit_price_uzs", "Infinity"),
        ("unit_price_uzs", "1e3"),
        ("is_free", "true"),
        ("price_change_confirmed", 1),
        ("free_reason", False),
    ],
)
def test_invalid_line_is_atomic(invoice_data, field, value):
    data = invoice_data
    before = ledger_state(data)
    payload = copy.deepcopy(data.payload)
    payload["lines"][0][field] = value
    response = post(data, payload)
    assert response.status_code == 422, response.content
    assert response.json()["errors"]
    assert ledger_state(data) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("currency", "USD"),
        ("declared_total_uzs", 499999),
        ("declared_total_uzs", True),
        ("declared_total_uzs", Decimal(".5")),
        ("invoice_date", "2026-02-30"),
        ("invoice_date", "2099-01-01"),
        ("notes", "x" * 1001),
        ("lines", []),
        ("branch_id", "evil"),
    ],
)
def test_invalid_header_is_atomic(invoice_data, field, value):
    data = invoice_data
    before = ledger_state(data)
    payload = copy.deepcopy(data.payload)
    payload[field] = value
    response = post(data, payload)
    assert response.status_code == 422, response.content
    assert ledger_state(data) == before


@pytest.mark.parametrize("raw", ["1e5", "NaN", "Infinity", "-Infinity"])
def test_non_plain_json_numbers_are_rejected(invoice_data, raw):
    data = invoice_data
    before = ledger_state(data)
    body = json_numbers.dumps(data.payload).replace(
        '"unit_price_uzs":100000', '"unit_price_uzs":' + raw
    )
    response = data.client.post(
        BASE + "receive/",
        body,
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="raw-number",
    )
    assert response.status_code == 422
    assert "lines.0.unit_price_uzs" in response.json()["errors"]
    assert ledger_state(data) == before


def test_key_required_and_payload_conflict(invoice_data):
    data = invoice_data
    assert post(data, key="").json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert post(data).status_code == 201
    after = ledger_state(data)
    payload = copy.deepcopy(data.payload)
    payload["notes"] = "changed"
    response = post(data, payload)
    assert (
        response.status_code == 409
        and response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    )
    assert ledger_state(data) == after


def test_duplicate_number_and_line(invoice_data):
    data = invoice_data
    payload = copy.deepcopy(data.payload)
    payload["lines"] *= 2
    payload["declared_total_uzs"] *= 2
    assert post(data, payload).json()["code"] == "DUPLICATE_INVOICE_LINE"
    assert post(data).status_code == 201
    payload = copy.deepcopy(data.payload)
    payload["supplier_invoice_number"] = "  inv-104  "
    response = post(data, payload, key="different-key")
    assert (
        response.status_code == 409
        and response.json()["code"] == "DUPLICATE_SUPPLIER_INVOICE"
    )


def test_alternative_unit_and_per_line_rounding(invoice_data):
    data = invoice_data
    bag = StockUnit.objects.create(
        name="Bag",
        short_name="bag",
        unit_type="COUNT",
        decimal_places=3,
        branch_id="cloud",
    )
    StockItemUnit.objects.create(
        stock_item=data.item, unit=bag, conversion_to_base=3, branch_id="branch1"
    )
    data.link.unit = bag
    data.link.price = 1
    data.link.price_is_known = False
    data.link.pack_size = 99
    data.link.save()
    payload = copy.deepcopy(data.payload)
    payload["lines"][0].update(quantity=Decimal("1.5"), unit_price_uzs=1)
    payload["declared_total_uzs"] = 2
    response = post(data, payload)
    assert response.status_code == 201, response.content
    line = json_numbers.loads(response.content)["data"]["invoice"]["lines"][0]
    assert line["base_quantity"] == Decimal("4.5")
    assert line["base_unit_cost_uzs"] == Decimal(".4444")
    assert line["line_total_uzs"] == 2
    movement = StockTransaction.objects.get(pk=line["stock_transaction_id"])
    assert movement.total_cost == 2  # not 4.5 * 0.4444


def test_unknown_price_marker_and_free_goods(invoice_data):
    data = invoice_data
    data.link.price = 0
    data.link.price_is_known = False
    data.link.notes = "Keep this. " + UNKNOWN_PRICE_MARKER + "\nAlso keep this."
    data.link.save()
    catalog = data.client.get(
        f"/api/admins/stock/suppliers/{data.supplier.id}/receivable-items/"
    ).json()
    assert catalog["data"]["items"][0]["suggested_unit_price_uzs"] is None
    assert post(data).status_code == 201
    data.link.refresh_from_db()
    assert data.link.price == 100000 and data.link.price_is_known
    assert data.link.notes == "Keep this. \nAlso keep this."
    payload = copy.deepcopy(data.payload)
    payload["supplier_invoice_number"] = "FREE-1"
    payload["declared_total_uzs"] = 0
    payload["lines"][0].update(unit_price_uzs=0, is_free=True, free_reason="Promotion")
    response = post(data, payload, key="free-goods")
    assert response.status_code == 201, response.content
    data.link.refresh_from_db()
    data.supplier.refresh_from_db()
    assert data.link.price == 100000 and data.supplier.current_balance == 700000
    assert (
        SupplierTransaction.objects.get(
            pk=response.json()["data"]["invoice"]["supplier_transaction_id"]
        ).amount
        == 0
    )


def test_price_confirmation_threshold(invoice_data):
    data = invoice_data
    before = ledger_state(data)
    payload = copy.deepcopy(data.payload)
    payload["lines"][0]["unit_price_uzs"] = 104000
    payload["declared_total_uzs"] = 520000
    response = post(data, payload)
    assert (
        response.status_code == 409
        and response.json()["code"] == "PRICE_CHANGE_CONFIRMATION_REQUIRED"
    )
    assert response.json()["details"]["percentage"] == 30
    assert ledger_state(data) == before
    payload["lines"][0].update(
        price_change_confirmed=True, price_change_reason="Invoice price confirmed"
    )
    assert post(data, payload).status_code == 201


def test_missing_old_cost_basis_and_proven_free_basis(invoice_data):
    data = invoice_data
    data.item.avg_cost_price = 0
    data.item.save()
    before = ledger_state(data)
    response = post(data)
    assert (
        response.status_code == 409
        and response.json()["code"] == "STOCK_COST_BASIS_MISSING"
    )
    assert ledger_state(data) == before
    data.level.quantity = 0
    data.level.save()
    free = copy.deepcopy(data.payload)
    free["supplier_invoice_number"] = "FREE"
    free["declared_total_uzs"] = 0
    free["lines"][0].update(unit_price_uzs=0, is_free=True, free_reason="Promo")
    assert post(data, free, key="free").status_code == 201
    response = post(data)
    assert response.status_code == 201, response.content
    data.item.refresh_from_db()
    assert data.item.avg_cost_price == 50000


def test_full_reversal_and_retry(invoice_data):
    data = invoice_data
    before = ledger_state(data)
    posted = post(data)
    assert posted.status_code == 201, posted.content
    invoice_id = posted.json()["data"]["invoice"]["id"]
    original = PurchaseReceiving.objects.get(pk=invoice_id).posting_manifest
    response = data.manager_client.post(
        BASE + f"{invoice_id}/reverse/",
        '{"reason":"Wrong invoice"}',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="reverse-1",
    )
    assert response.status_code == 200, response.content
    assert response.json()["data"]["invoice"]["status"] == "REVERSED"
    after = ledger_state(data)
    assert (
        after["quantity"] == before["quantity"]
        and after["balance"] == before["balance"]
    )
    assert after["average"] == before["average"] and after["price"] == before["price"]
    assert PurchaseReceiving.objects.get(pk=invoice_id).posting_manifest == original
    replay = data.manager_client.post(
        BASE + f"{invoice_id}/reverse/",
        '{"reason":"Wrong invoice"}',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="reverse-1",
    )
    assert replay.content == response.content and ledger_state(data) == after
    assert (
        post(data, key="duplicate-after-reversal").json()["code"]
        == "DUPLICATE_SUPPLIER_INVOICE"
    )


def test_consumed_then_replenished_stock_blocks_reversal(invoice_data):
    from stock.services.level_service import StockLevelService

    data = invoice_data
    response = post(data)
    assert response.status_code == 201, response.content
    invoice_id = response.json()["data"]["invoice"]["id"]
    for movement in ("SALE_OUT", "ADJUSTMENT_PLUS"):
        _, status = StockLevelService.adjust(
            data.item.id, data.location.id, 1, movement, data.manager.id
        )
        assert status == 200
    before = ledger_state(data)
    reversal = data.manager_client.post(
        BASE + f"{invoice_id}/reverse/",
        '{"reason":"Correction"}',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="reverse-consumed",
    )
    assert (
        reversal.status_code == 409
        and reversal.json()["code"] == "INVOICE_REVERSAL_STOCK_CONSUMED"
    )
    assert ledger_state(data) == before


def test_failed_second_movement_rolls_back_everything(invoice_data):
    from stock.services.level_service import StockLevelService

    data = invoice_data
    second = StockItem.objects.create(
        name="Second item", base_unit=data.kg, item_type="RAW", branch_id="branch1"
    )
    link = SupplierStockItem.objects.create(
        supplier=data.supplier,
        stock_item=second,
        unit=data.kg,
        price=100000,
        price_is_known=True,
        branch_id="branch1",
    )
    payload = copy.deepcopy(data.payload)
    payload["lines"].append(dict(payload["lines"][0], supplier_item_id=link.id))
    payload["declared_total_uzs"] = 1000000
    before = ledger_state(data)
    original = StockLevelService.adjust

    def fail_second(*args, **kwargs):
        if kwargs.get("stock_item_id") == second.id:
            return {
                "success": False,
                "code": "TEST_LATE_FAILURE",
                "message": "Late failure",
            }, 409
        return original(*args, **kwargs)

    with patch.object(StockLevelService, "adjust", side_effect=fail_second):
        response = post(data, payload)
    assert response.status_code == 409 and ledger_state(data) == before
    assert not StockLevel.objects.filter(stock_item=second).exists()


def test_permissions_balance_redaction_and_immutability(invoice_data):
    data = invoice_data
    posted = post(data)
    assert posted.status_code == 201, posted.content
    invoice_id = posted.json()["data"]["invoice"]["id"]
    assert (
        data.client.post(
            BASE + f"{invoice_id}/reverse/",
            '{"reason":"No"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="warehouse-reverse",
        ).status_code
        == 403
    )
    data.warehouse.permissions = [
        p for p in data.warehouse.permissions if p != "stock.supplier.balance.view"
    ]
    data.warehouse.save()
    detail = (
        _client(data.warehouse).get(BASE + f"{invoice_id}/").json()["data"]["invoice"]
    )
    assert "supplier_balance_before_uzs" not in detail
    receipt = PurchaseReceiving.objects.get(pk=invoice_id)
    receipt.notes = "Edited"
    with pytest.raises(TypeError):
        receipt.save()
    line = receipt.items.get()
    line.quantity_received = 6
    with pytest.raises(TypeError):
        line.save()
    with pytest.raises(TypeError):
        line.delete()
    assert (
        data.client.get(
            f"/api/admins/stock/purchase-orders/{receipt.purchase_order_id}/"
        ).status_code
        == 404
    )
    assert (
        data.client.get("/api/admins/stock/purchase-orders/").json()["data"]["orders"]
        == []
    )
