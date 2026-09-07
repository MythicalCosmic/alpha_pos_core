import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from base.models import User
from stock.models import (
    PurchaseReceiving,
    PurchaseReceivingItem,
    StockItem,
    StockLevel,
    Supplier,
    SupplierStockItem,
    SupplierTransaction,
)
from stock.services.purchase_invoices.validation import UNKNOWN_PRICE_MARKER

pytestmark = pytest.mark.django_db(transaction=True)


def financial_values():
    return {
        "levels": list(
            StockLevel.objects.order_by("id").values_list(
                "id", "quantity", "reserved_quantity"
            )
        ),
        "items": list(
            StockItem.objects.order_by("id").values_list(
                "id", "cost_price", "avg_cost_price", "last_cost_price"
            )
        ),
        "suppliers": list(
            Supplier.objects.order_by("id").values_list("id", "current_balance")
        ),
        "ledger": list(
            SupplierTransaction.objects.order_by("id").values_list(
                "id", "amount", "balance_before", "balance_after"
            )
        ),
    }


def test_migration_backfills_identity_and_price_state_without_financial_rewrites(
    invoice_data,
):
    data = invoice_data
    data.link.price = 0
    data.link.notes = "Catalog confirmed 2026-09-05. " + UNKNOWN_PRICE_MARKER
    data.link.save()
    data.warehouse.permissions = ["stock.catalog.view"]
    data.warehouse.save()
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    previous = [
        ("stock", "0017_secure_stock_adjustments"),
        ("base", "0064_repair_treasury_branch_scope"),
    ]
    try:
        executor.migrate(previous)
        apps = executor.loader.project_state(previous).apps
        Order = apps.get_model("stock", "PurchaseOrder")
        OrderLine = apps.get_model("stock", "PurchaseOrderItem")
        Receiving = apps.get_model("stock", "PurchaseReceiving")
        Line = apps.get_model("stock", "PurchaseReceivingItem")
        Item = apps.get_model("stock", "StockItem")
        Link = apps.get_model("stock", "SupplierStockItem")
        order = Order.objects.create(
            order_number="LEGACY-1",
            supplier_id=data.supplier.id,
            delivery_location_id=data.location.id,
            order_date="2026-09-01",
            created_by_id=data.manager.id,
            total=500000,
            branch_id="branch1",
        )
        order_line = OrderLine.objects.create(
            purchase_order=order,
            stock_item_id=data.item.id,
            supplier_stock_item_id=data.link.id,
            unit_id=data.kg.id,
            quantity_ordered=5,
            unit_price=100000,
            total_price=500000,
            branch_id="branch1",
        )
        receiving = Receiving.objects.create(
            purchase_order=order,
            receiving_number="LEGACY-RCV-1",
            location_id=data.location.id,
            received_by_id=data.manager.id,
            received_date="2026-09-01",
            branch_id="branch1",
        )
        line = Line.objects.create(
            receiving=receiving,
            po_item=order_line,
            stock_item_id=data.item.id,
            unit_id=data.kg.id,
            quantity_received=5,
            unit_cost=100000,
            branch_id="branch1",
        )
        links = []
        for index, price in enumerate((0, 12345)):
            item = Item.objects.create(
                name=f"Legacy {index}",
                base_unit_id=data.kg.id,
                item_type="RAW",
                branch_id="branch1",
            )
            links.append(
                Link.objects.create(
                    supplier_id=data.supplier.id,
                    stock_item=item,
                    unit_id=data.kg.id,
                    price=price,
                    notes="Unrelated note",
                    branch_id="branch1",
                )
            )
        before = financial_values()
        output = StringIO()
        call_command("audit_direct_invoice_migration", stdout=output)
        report = json.loads(output.getvalue())
        assert (
            report["dry_run"]
            and report["supplier_links_with_exact_unknown_marker"] == 1
        )
        assert report["accounting_records_rewritten"] is False
        assert financial_values() == before
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        assert financial_values() == before
        posted = PurchaseReceiving.objects.get(pk=receiving.pk)
        assert (
            posted.source_type == "PURCHASE_ORDER"
            and posted.supplier_id == data.supplier.id
        )
        assert (
            posted.supplier_invoice_number == ""
            and posted.invoice_date is None
            and posted.posting_manifest == {}
        )
        assert (
            PurchaseReceivingItem.objects.get(pk=line.pk).supplier_stock_item_id
            == data.link.id
        )
        data.link.refresh_from_db()
        assert (
            data.link.price == 0
            and not data.link.price_is_known
            and data.link.price_source == "IMPORT_UNKNOWN"
        )
        assert (
            data.link.notes == "Catalog confirmed 2026-09-05. " + UNKNOWN_PRICE_MARKER
        )
        assert not SupplierStockItem.objects.get(pk=links[0].pk).price_is_known
        assert SupplierStockItem.objects.get(pk=links[1].pk).price_is_known
        data.warehouse.refresh_from_db()
        assert set(data.warehouse.permissions) == {
            "stock.catalog.view",
            "stock.purchase_invoice.view",
            "stock.purchase_invoice.receive",
        }
        assert User.objects.get(pk=data.manager.id).permissions
    finally:
        MigrationExecutor(connection).migrate(latest)
