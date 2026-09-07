"""Invoice costing and snapshots used inside the shared completion engine."""

from decimal import Decimal, ROUND_HALF_UP
from django.db.models import OuterRef, Subquery

from stock.models import (
    PurchaseReceivingItem,
    StockItem,
    StockLevel,
    StockTransaction,
    SupplierStockItem,
)
from .idempotency import fail
from .validation import QUANTITY_MAX, UNKNOWN_PRICE_MARKER


def actor_name(actor):
    return " ".join(
        part for part in (actor.first_name, actor.last_name) if part
    ).strip()


def prepare_costs(receiving, lines, base_values):
    ids = sorted({line.stock_item_id for line in lines})
    items = {
        item.id: item
        for item in StockItem.objects.select_for_update()
        .filter(
            id__in=ids,
            branch_id=receiving.branch_id,
            is_deleted=False,
            is_active=True,
        )
        .order_by("id")
    }
    levels = list(
        StockLevel.objects.select_for_update(of=("self",))
        .filter(
            stock_item_id__in=ids,
            branch_id=receiving.branch_id,
            location__branch_id=receiving.branch_id,
            location__is_deleted=False,
            is_deleted=False,
        )
        .order_by("stock_item_id", "location_id")
    )
    groups = {}
    for item_id in ids:
        item = items.get(item_id)
        if item is None:
            fail("SUPPLIER_ITEM_NOT_FOUND", "Stock item is no longer active.", 404)
        old_quantity = sum(
            (level.quantity for level in levels if level.stock_item_id == item_id),
            Decimal(0),
        )
        old_average = item.avg_cost_price
        known_zero = False
        if old_quantity > 0 and old_average == 0:
            latest = (
                StockTransaction.objects.filter(
                    stock_item=item,
                    branch_id=receiving.branch_id,
                    is_deleted=False,
                    movement_type="PURCHASE_IN",
                    reversal__isnull=True,
                )
                .order_by("-created_at", "-id")
                .values("invoice_line__invoice_snapshot")
                .first()
            )
            snapshot = (latest or {}).get("invoice_line__invoice_snapshot") or {}
            known_zero = (
                snapshot.get("cost_basis_known_after")
                and Decimal(snapshot.get("new_average_cost_uzs", "-1")) == 0
            )
        if (
            old_quantity < 0
            or not old_average.is_finite()
            or old_average < 0
            or (old_quantity > 0 and old_average == 0 and not known_zero)
        ):
            fail(
                "STOCK_COST_BASIS_MISSING",
                "Existing stock has no verified average cost basis.",
                409,
                details={"stock_item_id": item_id},
            )
        item_lines = [line for line in lines if line.stock_item_id == item_id]
        quantity = sum(
            (base_values[line.id]["quantity"] for line in item_lines), Decimal(0)
        )
        value = sum((line.line_total_uzs for line in item_lines), Decimal(0))
        destination_quantity = sum(
            (
                level.quantity
                for level in levels
                if level.stock_item_id == item_id
                and level.location_id == receiving.location_id
            ),
            Decimal(0),
        )
        if destination_quantity + quantity > QUANTITY_MAX:
            fail(
                "VALIDATION_ERROR",
                "Destination stock quantity exceeds capacity.",
                details={"stock_item_id": item_id},
            )
        new_average = (
            (old_quantity * old_average + value) / (old_quantity + quantity)
        ).quantize(
            Decimal(".0001"),
            rounding=ROUND_HALF_UP,
        )
        if new_average > QUANTITY_MAX:
            fail(
                "VALIDATION_ERROR",
                "Average cost exceeds capacity.",
                details={"stock_item_id": item_id},
            )
        groups[item_id] = {
            "item": item,
            "old_quantity": old_quantity,
            "old_average": old_average,
            "old_last": item.last_cost_price,
            "new_average": new_average,
            "received_quantity": quantity,
            "received_value": value,
            "last_cost": base_values[max(item_lines, key=lambda row: row.id).id][
                "unit_cost"
            ],
        }
    return groups


def save_posted_line(line, base, movement, group):
    snapshot = dict(line.invoice_snapshot)
    snapshot.update(
        {
            "base_quantity": str(base["quantity"]),
            "base_unit_cost_uzs": str(base["unit_cost"]),
            "conversion_to_base": str(base["factor"]),
            "stock_quantity_before": movement["quantity_before"],
            "stock_quantity_after": movement["quantity_after"],
            "previous_average_cost_uzs": str(group["old_average"]),
            "new_average_cost_uzs": str(group["new_average"]),
            "previous_global_quantity": str(group["old_quantity"]),
            "previous_last_cost_uzs": str(group["old_last"]),
            "cost_basis_known_after": True,
            "stock_transaction_id": movement["transaction_id"],
        }
    )
    line.stock_transaction_id = movement["transaction_id"]
    line.invoice_snapshot = snapshot
    line.save(update_fields=["stock_transaction", "invoice_snapshot"])


def apply_grouped_costs(groups):
    for item_id in sorted(groups):
        group = groups[item_id]
        item = group["item"]
        item.avg_cost_price = group["new_average"]
        item.last_cost_price = group["last_cost"]
        item.save(update_fields=["avg_cost_price", "last_cost_price", "updated_at"])


def refresh_supplier_prices(link_ids):
    """Recompute suggestions by business date, posting time, invoice, and lot ID."""
    base = PurchaseReceivingItem.objects.filter(
        supplier_stock_item_id=OuterRef("pk"),
        is_deleted=False,
        is_free=False,
        receiving__is_deleted=False,
        receiving__source_type="DIRECT_INVOICE",
        receiving__status="COMPLETED",
        unit_cost__gt=0,
    )
    latest = base.filter(receiving__reversed_at__isnull=True).order_by(
        "-receiving__invoice_date",
        "-receiving__completed_at",
        "-receiving_id",
        "-id",
    )
    earliest = base.order_by("receiving__completed_at", "receiving_id", "id")
    links = list(
        SupplierStockItem.objects.select_for_update()
        .filter(id__in=sorted(link_ids))
        .annotate(
            latest_line=Subquery(latest.values("id")[:1]),
            baseline_line=Subquery(earliest.values("id")[:1]),
        )
        .order_by("id")
    )
    ids = {pk for link in links for pk in (link.latest_line, link.baseline_line) if pk}
    lines = {
        line.id: line
        for line in PurchaseReceivingItem.objects.filter(id__in=ids).select_related(
            "receiving"
        )
    }
    for link in links:
        if link.latest_line:
            line = lines[link.latest_line]
            link.price = line.unit_cost
            link.currency = "UZS"
            link.price_is_known = True
            link.price_source = "INVOICE"
            link.price_source_invoice_uuid = line.receiving.uuid
            link.last_price_update = line.receiving.completed_at
            link.notes = link.notes.replace(UNKNOWN_PRICE_MARKER, "").strip()
        elif link.baseline_line:
            baseline = lines[link.baseline_line].invoice_snapshot[
                "supplier_price_before"
            ]
            link.price = Decimal(baseline["price"])
            link.currency = baseline["currency"]
            link.price_is_known = baseline["known"]
            link.price_source = baseline["source"]
            link.price_source_invoice_uuid = None
            link.last_price_update = None
            if baseline["last_price_update"]:
                from django.utils.dateparse import parse_datetime

                link.last_price_update = parse_datetime(baseline["last_price_update"])
        else:
            continue  # All-free invoices never replace supplier price knowledge.
        link.save(
            update_fields=[
                "price",
                "currency",
                "price_is_known",
                "price_source",
                "price_source_invoice_uuid",
                "last_price_update",
                "notes",
                "updated_at",
            ]
        )


def finalize(receiving, actor):
    from .serialization import posted_manifest

    receiving.refresh_from_db()
    lines = list(
        receiving.items.filter(is_deleted=False, po_item__is_deleted=False).order_by(
            "id"
        )
    )
    refresh_supplier_prices(
        {line.supplier_stock_item_id for line in lines if not line.is_free}
    )
    receiving.posted_by = actor
    receiving.posting_manifest = {
        "version": 1,
        "invoice": posted_manifest(receiving, lines, actor),
    }
    receiving.save(update_fields=["posted_by", "posting_manifest", "updated_at"])
    return receiving
