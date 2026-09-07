from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from stock.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceiving,
    PurchaseReceivingItem,
    StockTransaction,
    SupplierTransaction,
)
from stock.services.purchase_service import (
    PurchaseOrderItemService,
    PurchaseOrderService,
    PurchaseReceivingService,
)
from .helpers import ledger_state, post

pytestmark = pytest.mark.django_db


def planned(data, lines=None):
    return PurchaseOrderService.create(
        supplier_id=data.supplier.id,
        delivery_location_id=data.location.id,
        order_date="2026-09-06",
        created_by_id=data.manager.id,
        branch_id="branch1",
        items=lines
        if lines is not None
        else [
            {
                "stock_item_id": data.item.id,
                "supplier_stock_item_id": data.link.id,
                "unit_id": data.kg.id,
                "quantity": 5,
                "unit_price": 100000,
            }
        ],
    )


def receipt(data, po_id):
    po = PurchaseOrder.objects.get(pk=po_id)
    po.status = "CONFIRMED"
    po.save()
    body, status = PurchaseReceivingService.create(
        po_id, data.manager.id, data.location.id, received_date="2026-09-06"
    )
    assert status == 200, body
    return PurchaseReceiving.objects.get(pk=body["data"]["id"])


def test_nested_po_dates_supplier_identity_and_saved_precision(invoice_data):
    data = invoice_data
    body, status = planned(data)
    assert status == 200, body
    po = PurchaseOrder.objects.get(pk=body["data"]["id"])
    line = po.items.get()
    assert po.order_date.isoformat() == "2026-09-06"
    assert timezone.localtime(po.payment_due_date).date().isoformat() == "2026-10-06"
    assert line.supplier_stock_item_id == data.link.id
    assert body["data"]["order"]["items"][0]["supplier_stock_item_id"] == data.link.id
    assert (
        PurchaseOrderItemService.update_item(line.id, notes="Update only the note")[1]
        == 200
    )
    receiving = receipt(data, po.id)
    body, status = PurchaseReceivingService.add_item(
        receiving.id, line.id, 5, expiry_date="2026-10-01"
    )
    assert status == 200, body
    item = receiving.items.get()
    assert (
        item.supplier_stock_item_id == data.link.id
        and item.expiry_date.isoformat() == "2026-10-01"
    )
    assert PurchaseReceivingService.complete(receiving.id, actor=data.manager)[1] == 200


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", 0),
        ("quantity", -1),
        ("quantity", True),
        ("unit_price", 0),
        ("unit_price", -1),
        ("discount_percent", 101),
        ("discount_percent", -1),
        ("tax_percent", "NaN"),
        ("tax_percent", 101),
        ("quantity", Decimal(".0001")),
        ("supplier_stock_item_id", 99999),
    ],
)
def test_bad_last_nested_po_line_rolls_back_whole_aggregate(invoice_data, field, value):
    data = invoice_data
    line = {
        "stock_item_id": data.item.id,
        "supplier_stock_item_id": data.link.id,
        "unit_id": data.kg.id,
        "quantity": 5,
        "unit_price": 100000,
    }
    before = ledger_state(data)
    body, status = planned(data, [line, dict(line, **{field: value})])
    assert status == 422, body
    assert ledger_state(data) == before and not PurchaseOrderItem.objects.exists()


@pytest.mark.parametrize("deleted_kind", ["receiving_line", "order_line"])
def test_soft_deleted_lines_never_serialize_total_or_post(invoice_data, deleted_kind):
    data = invoice_data
    raw = {
        "stock_item_id": data.item.id,
        "supplier_stock_item_id": data.link.id,
        "unit_id": data.kg.id,
        "quantity": 5,
        "unit_price": 100000,
    }
    body, status = planned(data, [raw, raw])
    assert status == 200
    po = PurchaseOrder.objects.get(pk=body["data"]["id"])
    receiving = receipt(data, po.id)
    po_lines = list(po.items.order_by("id"))
    for line in po_lines:
        assert PurchaseReceivingService.add_item(receiving.id, line.id, 5)[1] == 200
    receiving_lines = list(receiving.items.order_by("id"))
    (receiving_lines[1] if deleted_kind == "receiving_line" else po_lines[1]).delete()
    if deleted_kind == "order_line":
        PurchaseOrderService._recalculate_totals(po.id)
        po.refresh_from_db()
        assert (
            po.total == 500000 and len(PurchaseOrderService.serialize(po)["items"]) == 1
        )
    assert len(PurchaseReceivingService.serialize(receiving)["items"]) == 1
    body, status = PurchaseReceivingService.complete(receiving.id, actor=data.manager)
    assert status == 200, body
    data.level.refresh_from_db()
    data.supplier.refresh_from_db()
    assert data.level.quantity == 15 and data.supplier.current_balance == 700000
    assert StockTransaction.objects.count() == 1
    assert PurchaseOrderItem.objects.get(pk=po_lines[1].id).quantity_received == 0


def test_deleted_only_order_cannot_send_and_foreign_target_is_not_empty_success(
    invoice_data,
):
    data = invoice_data
    body, status = planned(data)
    assert status == 200
    po = PurchaseOrder.objects.get(pk=body["data"]["id"])
    po.items.get().delete()
    assert PurchaseOrderService.send(po.id)[1] == 400
    po.branch_id = "foreign"
    po.save()
    response = data.client.get(f"/api/admins/stock/purchase-order/{po.id}/receiving/")
    assert response.status_code == 404


def test_receiving_update_revalidates_dates_catalog_and_quantity_precision(
    invoice_data,
):
    data = invoice_data
    body, status = planned(data)
    assert status == 200
    po = PurchaseOrder.objects.get(pk=body["data"]["id"])
    receiving = receipt(data, po.id)
    body, status = PurchaseReceivingService.add_item(receiving.id, po.items.get().id, 5)
    assert status == 200
    line = receiving.items.get()
    assert (
        PurchaseReceivingService.update_item(line.id, expiry_date="2026-09-05")[1]
        == 422
    )
    assert (
        PurchaseReceivingService.update_item(
            line.id, quantity_received=Decimal(".0001")
        )[1]
        == 422
    )
    data.item.is_active = False
    data.item.save()
    assert (
        PurchaseReceivingService.update_item(line.id, notes="Catalog changed")[1] == 422
    )
    line.refresh_from_db()
    assert line.expiry_date is None and line.quantity_received == 5


@override_settings(DEPLOYMENT_MODE="cloud")
def test_branch_sync_cannot_rewrite_or_extend_cloud_invoice(invoice_data):
    from base.services.sync.receiver import CloudReceiver

    data = invoice_data
    posted = post(data)
    assert posted.status_code == 201
    receipt = PurchaseReceiving.objects.get(pk=posted.json()["data"]["invoice"]["id"])
    before = ledger_state(data)
    for record in (
        receipt.purchase_order,
        receipt.purchase_order.items.get(),
        receipt,
        receipt.items.get(),
    ):
        payload = record.to_sync_dict()
        payload.update(sync_version=record.sync_version + 50, notes="Peer rewrite")
        payload.pop("source_type", None)  # An older desktop does not know this column.
        result = CloudReceiver._create_or_update(type(record), payload, "branch1")
        assert result.reason_code == "INVOICE_COMMAND_REQUIRED"
        record.refresh_from_db()
        assert record.notes != "Peer rewrite"
    line = receipt.items.get()
    payload = line.to_sync_dict()
    payload.update(uuid=str(uuid4()), sync_version=1)
    result = CloudReceiver._create_or_update(PurchaseReceivingItem, payload, "branch1")
    assert result.reason_code == "INVOICE_COMMAND_REQUIRED"
    assert ledger_state(data) == before


def test_ledger_identity_and_reference_have_database_uniqueness(invoice_data):
    data = invoice_data
    posted = post(data)
    assert posted.status_code == 201
    receiving = PurchaseReceiving.objects.get(pk=posted.json()["data"]["invoice"]["id"])
    original = receiving.supplier_transaction
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierTransaction.objects.create(
            supplier=data.supplier,
            type="PURCHASE",
            amount=500000,
            invoice_posting_id=uuid4(),
            reference_type=original.reference_type,
            reference_id=original.reference_id,
            branch_id="branch1",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierTransaction.objects.create(
            supplier=data.supplier,
            type="PURCHASE",
            amount=500000,
            invoice_posting_id=original.invoice_posting_id,
            reference_type="Other",
            reference_id=123,
            branch_id="branch1",
        )
    movement = receiving.items.get().stock_transaction
    assert movement.command_id is not None
    for record in (original, movement):
        payload = record.to_sync_dict()
        payload.update(sync_version=record.sync_version + 100, is_deleted=True)
        before = type(record).objects.get(pk=record.pk).to_sync_dict()
        _, action = type(record).from_sync_dict(payload, branch_id="branch1")
        assert action == "skipped"
        assert type(record).objects.get(pk=record.pk).to_sync_dict() == before
