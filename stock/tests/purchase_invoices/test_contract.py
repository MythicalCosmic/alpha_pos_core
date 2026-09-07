"""Contract boundaries beyond the basic posting calculation."""

import copy
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from base.models import User
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from stock.models import (
    PurchaseReceiving,
    PurchaseReceivingItem,
    StockBatch,
    StockItem,
    StockItemUnit,
    StockUnit,
    Supplier,
    SupplierStockItem,
    SupplierTransaction,
)
from stock.services.purchase_invoices import json_numbers, queries
from stock.services.purchase_service import PurchaseReceivingService
from .helpers import BASE, ledger_state, post
from stock.tests.test_warehouse_receiving import _client

pytestmark = pytest.mark.django_db


def reverse(data, invoice_id, key="reverse", reason="Correction"):
    return data.manager_client.post(
        BASE + f"{invoice_id}/reverse/",
        json_numbers.dumps({"reason": reason}),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=key,
    )


@pytest.mark.parametrize("quantity", [100, Decimal("10.000"), Decimal("1.2500")])
def test_quantity_granularity_ignores_insignificant_zeros(invoice_data, quantity):
    data = invoice_data
    payload = copy.deepcopy(data.payload)
    payload["lines"][0]["quantity"] = quantity
    payload["declared_total_uzs"] = int(quantity * 100000)
    response = post(data, payload)
    assert response.status_code == 201, response.content


def test_multiple_items_and_lots_post_once_with_grouped_cost(invoice_data):
    data = invoice_data
    data.item.track_batches = True
    data.item.track_expiry = True
    data.item.save()
    second = StockItem.objects.create(
        name="Packaging", base_unit=data.kg, item_type="PACKAGING", branch_id="branch1"
    )
    second_link = SupplierStockItem.objects.create(
        supplier=data.supplier,
        stock_item=second,
        unit=data.kg,
        price=10,
        price_is_known=True,
        branch_id="branch1",
    )
    payload = copy.deepcopy(data.payload)
    payload["lines"][0].update(batch_number="LOT-A", expiry_date="2026-10-01")
    payload["lines"].append(
        dict(
            payload["lines"][0], quantity=2, unit_price_uzs=90000, batch_number="LOT-B"
        )
    )
    payload["lines"].append(
        {"supplier_item_id": second_link.id, "quantity": 3, "unit_price_uzs": 10}
    )
    payload["declared_total_uzs"] = 680030
    response = post(data, payload)
    assert response.status_code == 201, response.content
    invoice = json_numbers.loads(response.content)["data"]["invoice"]
    assert invoice["line_count"] == 3
    assert len({line["stock_transaction_id"] for line in invoice["lines"]}) == 3
    data.item.refresh_from_db()
    data.link.refresh_from_db()
    second.refresh_from_db()
    assert data.item.avg_cost_price == Decimal("87058.8235")
    assert data.item.last_cost_price == data.link.price == 90000
    assert second.avg_cost_price == 10
    assert StockBatch.objects.count() == 2
    assert SupplierTransaction.objects.filter(type="PURCHASE").count() == 1
    assert all(line["quality_status"] == "PASSED" for line in invoice["lines"])
    assert reverse(data, invoice["id"]).status_code == 200
    assert list(StockBatch.objects.values_list("current_quantity", flat=True)) == [0, 0]
    data.item.refresh_from_db()
    assert data.item.avg_cost_price == 80000


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("batch_number", "", "BATCH_REQUIRED"),
        ("expiry_date", None, "EXPIRY_REQUIRED"),
        ("expiry_date", "2026-09-06", "VALIDATION_ERROR"),
        ("expiry_date", "2026-02-30", "VALIDATION_ERROR"),
        ("quality_status", "FAILED", "VALIDATION_ERROR"),
    ],
)
def test_batch_expiry_and_passed_only_inputs(invoice_data, field, value, code):
    data = invoice_data
    data.item.track_batches = True
    data.item.track_expiry = True
    data.item.save()
    payload = copy.deepcopy(data.payload)
    payload["lines"][0].update(batch_number="LOT-A", expiry_date="2026-10-01")
    payload["lines"][0][field] = value
    before = ledger_state(data)
    response = post(data, payload)
    assert (
        response.status_code == 422 and response.json()["code"] == code
    ), response.content
    assert ledger_state(data) == before


def test_expiry_only_items_get_a_traceable_batch(invoice_data):
    data = invoice_data
    data.item.track_expiry = True
    data.item.save()
    payload = copy.deepcopy(data.payload)
    payload["lines"][0]["expiry_date"] = "2026-10-01"
    response = post(data, payload)
    assert response.status_code == 201, response.content
    line = response.json()["data"]["invoice"]["lines"][0]
    assert line["batch_id"] is not None
    assert (
        StockBatch.objects.get(pk=line["batch_id"]).expiry_date.isoformat()
        == "2026-10-01"
    )


@pytest.mark.parametrize(
    "entity,field,value",
    [
        ("supplier", "is_active", False),
        ("supplier", "is_deleted", True),
        ("supplier", "branch_id", "foreign"),
        ("location", "is_active", False),
        ("location", "is_deleted", True),
        ("location", "branch_id", "foreign"),
        ("link", "is_active", False),
        ("link", "is_deleted", True),
        ("link", "branch_id", "foreign"),
        ("item", "is_active", False),
        ("item", "is_deleted", True),
        ("item", "is_purchasable", False),
        ("item", "branch_id", "foreign"),
        ("kg", "is_active", False),
        ("kg", "is_deleted", True),
    ],
)
def test_inactive_deleted_and_foreign_catalog_never_posts(
    invoice_data, entity, field, value
):
    data = invoice_data
    obj = getattr(data, entity)
    setattr(obj, field, value)
    obj.save()
    before = ledger_state(data)
    response = post(data)
    assert response.status_code in (403, 404, 422), response.content
    assert response.json()["errors"]
    assert ledger_state(data) == before
    catalog = data.client.get(
        f"/api/admins/stock/suppliers/{data.supplier.id}/receivable-items/"
    )
    if entity not in ("supplier", "location"):
        assert catalog.json()["data"]["items"] == []


def test_supplier_mismatch_and_missing_conversion(invoice_data):
    data = invoice_data
    other = Supplier.objects.create(name="Other", branch_id="branch1")
    payload = copy.deepcopy(data.payload)
    payload["supplier_id"] = other.id
    assert post(data, payload).json()["code"] == "SUPPLIER_ITEM_MISMATCH"
    bag = StockUnit.objects.create(
        name="Bag", short_name="bag", unit_type="COUNT", branch_id="cloud"
    )
    data.link.unit = bag
    data.link.save()
    assert post(data).json()["code"] == "UNIT_CONVERSION_MISSING"
    # An override attached to the wrong branch cannot authorize a conversion.
    StockItemUnit.objects.create(
        stock_item=data.item, unit=bag, conversion_to_base=5, branch_id="foreign"
    )
    assert post(data).json()["code"] == "UNIT_CONVERSION_MISSING"
    assert not PurchaseReceiving.objects.exists()


def test_backdated_prices_reversal_and_linked_replacement(invoice_data):
    data = invoice_data
    newer = post(data).json()["data"]["invoice"]
    original_lines = newer["lines"]
    payload = copy.deepcopy(data.payload)
    payload.update(
        supplier_invoice_number="OLD",
        invoice_date="2026-09-05",
        declared_total_uzs=450000,
    )
    payload["lines"][0]["unit_price_uzs"] = 90000
    older = post(data, payload, key="backdated")
    assert older.status_code == 201, older.content
    data.link.refresh_from_db()
    assert data.link.price == 100000
    assert (
        data.link.price_source_invoice_uuid
        == PurchaseReceiving.objects.get(pk=newer["id"]).uuid
    )
    assert reverse(data, newer["id"]).status_code == 200
    data.link.refresh_from_db()
    assert data.link.price == 90000
    historical = data.client.get(BASE + f"{newer['id']}/").json()["data"]["invoice"]
    assert historical["lines"] == original_lines
    replacement = copy.deepcopy(data.payload)
    replacement.update(
        supplier_invoice_number="CORRECTED", replaces_invoice_id=newer["id"]
    )
    response = post(data, replacement, key="replacement")
    assert response.status_code == 201, response.content
    detail = data.client.get(BASE + f"{newer['id']}/").json()["data"]["invoice"]
    assert detail["replacement_invoice_ids"] == [
        response.json()["data"]["invoice"]["id"]
    ]


def test_empty_supplier_number_stays_empty_and_internal_number_is_visible(invoice_data):
    data = invoice_data
    payload = copy.deepcopy(data.payload)
    payload["supplier_invoice_number"] = "  "
    first = post(data, payload).json()["data"]["invoice"]
    second = post(data, payload, key="second-no-number").json()["data"]["invoice"]
    assert first["supplier_invoice_number"] == second["supplier_invoice_number"] == ""
    assert first["receiving_number"] != second["receiving_number"]


@pytest.mark.parametrize("failure_at", ["price", "ledger", "audit"])
def test_late_failures_rollback_all_accounting_and_hide_exception(
    invoice_data, failure_at
):
    data = invoice_data
    before = ledger_state(data)
    targets = {
        "price": "stock.services.purchase_invoices.posting.refresh_supplier_prices",
        "ledger": "stock.services.supplier_ledger_service.SupplierLedgerService.record_purchase",
        "audit": "stock.services.purchase_invoices.commands.AuditLog.objects.create",
    }
    with patch(
        targets[failure_at], side_effect=RuntimeError("private exception detail")
    ):
        response = post(data)
    assert response.status_code == 500
    assert response.json()["code"] == "INVOICE_OPERATION_FAILED"
    assert (
        b"private exception detail" not in response.content
        and b"Traceback" not in response.content
    )
    assert ledger_state(data) == before
    assert post(data).status_code == 201


def test_shared_completion_rejects_injected_unaccepted_direct_lines(invoice_data):
    data = invoice_data
    before = ledger_state(data)
    original = PurchaseReceivingService.complete

    def corrupt(receiving_id, **kwargs):
        PurchaseReceivingItem.objects.filter(receiving_id=receiving_id).update(
            quality_status="FAILED"
        )
        return original(receiving_id, **kwargs)

    with patch.object(PurchaseReceivingService, "complete", side_effect=corrupt):
        response = post(data)
    assert response.status_code == 422, response.content
    assert ledger_state(data) == before


def test_replay_cannot_restore_revoked_balance_permission(invoice_data):
    data = invoice_data
    response = post(data)
    assert response.status_code == 201
    data.warehouse.permissions = [
        p for p in data.warehouse.permissions if p != "stock.supplier.balance.view"
    ]
    data.warehouse.save()
    before = ledger_state(data)
    replay = post(data, client=_client(data.warehouse))
    assert replay.status_code == 201
    assert "supplier_balance_before_uzs" not in replay.json()["data"]["invoice"]
    assert ledger_state(data) == before


@pytest.mark.parametrize("role", ["WAREHOUSE", "MANAGER", "ADMIN"])
def test_role_matrix_and_cross_branch_ids(invoice_data, role):
    data = invoice_data
    actor = User.objects.create(
        first_name=role,
        email=f"{role}@matrix.test",
        password="!",
        role=role,
        status="ACTIVE",
        branch_id="branch1",
        permissions=DEFAULT_ROLE_PERMISSIONS.get(role, []),
    )
    client = _client(actor)
    posted = post(data, client=client)
    assert posted.status_code == 201, posted.content
    invoice_id = posted.json()["data"]["invoice"]["id"]
    assert (
        client.get(BASE).status_code
        == client.get(BASE + f"{invoice_id}/").status_code
        == 200
    )
    correction = client.post(
        BASE + f"{invoice_id}/reverse/",
        '{"reason":"Correction"}',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="matrix-reverse",
    )
    assert correction.status_code == (
        403 if role == "WAREHOUSE" else 200
    ), correction.content
    actor.branch_id = "foreign"
    actor.save()
    foreign = _client(actor)
    before = ledger_state(data)
    assert foreign.get(BASE + f"{invoice_id}/").status_code == 404
    assert post(data, client=foreign, key="foreign").status_code == 404
    assert (
        foreign.get(
            f"/api/admins/stock/suppliers/{data.supplier.id}/receivable-items/"
        ).status_code
        == 404
    )
    assert ledger_state(data) == before


@pytest.mark.parametrize("size", [1, 12])
def test_invoice_and_catalog_filters_precede_pagination_with_bounded_queries(
    invoice_data, size, record_property
):
    data = invoice_data
    for index in range(size):
        payload = copy.deepcopy(data.payload)
        payload["supplier_invoice_number"] = f"MATCH-{index:02d}"
        assert post(data, payload, key=f"match-{index}").status_code == 201
        item = StockItem.objects.create(
            name=f"Catalog match {index:02d}",
            base_unit=data.kg,
            item_type="RAW",
            branch_id="branch1",
        )
        SupplierStockItem.objects.create(
            supplier=data.supplier,
            stock_item=item,
            unit=data.kg,
            price=10,
            price_is_known=True,
            branch_id="branch1",
        )
    with CaptureQueriesContext(connection) as captured:
        body, status = queries.list_invoices(
            actor=data.warehouse,
            params={
                "search": "MATCH",
                "per_page": size,
                "supplier": data.supplier.id,
                "location": data.location.id,
                "stock_item": data.item.id,
                "creator": data.warehouse.id,
                "poster": data.warehouse.id,
                "status": "POSTED",
                "invoice_date_from": "2026-09-06",
                "invoice_date_to": "2026-09-06",
            },
        )
    assert status == 200 and body["data"]["pagination"]["total"] == size
    assert body["data"]["total_uzs"] == size * 500000
    assert len(captured) <= 5, [q["sql"] for q in captured]
    record_property("invoice_list_query_count", len(captured))
    with CaptureQueriesContext(connection) as captured:
        body, status = queries.receivable_items(
            actor=data.warehouse,
            supplier_id=data.supplier.id,
            params={"search": "Catalog match", "per_page": size},
        )
    assert status == 200 and body["data"]["pagination"]["total"] == size
    assert len(body["data"]["items"]) == size
    assert len(captured) <= 5, [q["sql"] for q in captured]
    record_property("receivable_catalog_query_count", len(captured))
    body, _ = queries.list_invoices(
        actor=data.warehouse, params={"search": "MATCH", "per_page": 1}
    )
    assert (
        len(body["data"]["invoices"]) == 1
        and body["data"]["total_uzs"] == size * 500000
    )


def test_posted_lines_and_stock_evidence_cannot_be_added_edited_or_deleted(
    invoice_data,
):
    data = invoice_data
    posted = post(data).json()["data"]["invoice"]
    receiving = PurchaseReceiving.objects.get(pk=posted["id"])
    line = receiving.items.get()
    with pytest.raises(TypeError):
        receiving.hard_delete()
    with pytest.raises(TypeError):
        line.hard_delete()
    with pytest.raises(TypeError):
        PurchaseReceivingItem.objects.create(
            receiving=receiving,
            po_item=line.po_item,
            stock_item=data.item,
            quantity_received=5,
            unit=data.kg,
            unit_cost=100000,
        )
    movement = line.stock_transaction
    movement.total_cost = 1
    with pytest.raises(TypeError):
        movement.save()
    with pytest.raises(TypeError):
        movement.delete()
    with pytest.raises(TypeError):
        movement.hard_delete()
    receiving.source_type = "PURCHASE_ORDER"
    receiving.posting_manifest = {}
    with pytest.raises(TypeError):
        receiving.hard_delete()
    line.receiving_id = 999999
    with pytest.raises(TypeError):
        line.save()
