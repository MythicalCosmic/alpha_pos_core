"""Append-only full reversal, conservatively blocked when consumption is possible."""

from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid5

from django.db.models import F, Q
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.models import AuditLog
from stock.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceiving,
    PurchaseReceivingCorrection,
    StockBatch,
    StockItem,
    StockLevel,
    StockLocation,
    StockTransaction,
    Supplier,
    SupplierStockItem,
)
from stock.services.level_service import StockLevelService
from stock.services.supplier_ledger_service import SupplierLedgerService
from .idempotency import InvoiceError, execute, fail
from .posting import refresh_supplier_prices
from .serialization import local_time, serialize
from .validation import MAX_INVOICE_VALUE, actor_scope, text


def reverse(*, actor, invoice_id, payload, idempotency_key):
    try:
        branch = actor_scope(actor, "stock.purchase_invoice.correct", correction=True)
        if not isinstance(payload, dict) or set(payload) != {"reason"}:
            fail(
                "VALIDATION_ERROR",
                "Provide only the mandatory reversal reason.",
                field="reason",
            )
        reason = text(payload["reason"], "reason", required=True)
        return execute(
            actor=actor,
            branch=branch,
            operation="reverse",
            target=invoice_id,
            key=idempotency_key,
            payload={"reason": reason},
            command=lambda action, key: _reverse(
                actor, branch, invoice_id, reason, action, key
            ),
        )
    except InvoiceError as exc:
        return exc.result


def _unavailable(
    items, message="The received stock cannot be proven available and unconsumed."
):
    fail(
        "INVOICE_REVERSAL_STOCK_CONSUMED",
        message,
        409,
        details={"affected_items": items},
    )


def _reverse(actor, branch, invoice_id, reason, action, key):
    identity = (
        PurchaseReceiving.objects.filter(
            pk=invoice_id,
            source_type="DIRECT_INVOICE",
            branch_id=branch,
            is_deleted=False,
        )
        .values("supplier_id", "purchase_order_id")
        .first()
    )
    if identity is None:
        fail("INVOICE_NOT_FOUND", "Invoice not found in this branch.", 404)
    supplier = (
        Supplier.objects.select_for_update()
        .filter(pk=identity["supplier_id"], branch_id=branch, is_deleted=False)
        .first()
    )
    if supplier is None:
        fail("SUPPLIER_NOT_FOUND", "Supplier no longer exists in this branch.", 404)
    po = PurchaseOrder.objects.select_for_update().get(pk=identity["purchase_order_id"])
    receiving = PurchaseReceiving.objects.select_for_update().get(
        pk=invoice_id, branch_id=branch, is_deleted=False
    )
    if receiving.reversed_at:
        fail("INVOICE_ALREADY_REVERSED", "This invoice has already been reversed.", 409)
    if receiving.status != "COMPLETED" or not receiving.posting_manifest:
        fail("INVOICE_NOT_POSTED", "Only posted invoices can be reversed.", 409)
    lines = list(
        receiving.items.filter(is_deleted=False, po_item__is_deleted=False)
        .select_related(
            "stock_transaction",
        )
        .order_by("stock_item_id", "id")
    )
    if len(lines) != receiving.posting_manifest["invoice"]["line_count"]:
        _unavailable(
            [], "The original invoice lines no longer match their posting manifest."
        )
    link_ids = sorted({line.supplier_stock_item_id for line in lines})
    list(
        SupplierStockItem.objects.select_for_update()
        .filter(pk__in=link_ids)
        .order_by("pk")
    )
    item_ids = sorted({line.stock_item_id for line in lines})
    items = {
        item.id: item
        for item in StockItem.objects.select_for_update()
        .filter(
            pk__in=item_ids,
            branch_id=branch,
            is_deleted=False,
            is_active=True,
        )
        .order_by("id")
    }
    location = (
        StockLocation.objects.select_for_update()
        .filter(
            pk=receiving.location_id,
            branch_id=branch,
            is_active=True,
            is_deleted=False,
        )
        .first()
    )
    if location is None or len(items) != len(item_ids):
        _unavailable(
            [{"stock_item_id": pk} for pk in item_ids],
            "Invoice inventory scope is no longer active.",
        )
    levels = list(
        StockLevel.objects.select_for_update(of=("self",))
        .filter(
            stock_item_id__in=item_ids,
            branch_id=branch,
            is_deleted=False,
            location__branch_id=branch,
            location__is_deleted=False,
        )
        .order_by("stock_item_id", "location_id")
    )
    batches = {
        batch.id: batch
        for batch in StockBatch.objects.select_for_update()
        .filter(
            pk__in=sorted(
                {line.batch_created_id for line in lines if line.batch_created_id}
            ),
            branch_id=branch,
            location=location,
            is_deleted=False,
        )
        .order_by("id")
    }
    affected, groups = [], {}
    for item_id in item_ids:
        item_lines = [line for line in lines if line.stock_item_id == item_id]
        quantity = sum((line.base_quantity for line in item_lines), Decimal(0))
        value = sum((line.line_total_uzs for line in item_lines), Decimal(0))
        target = next(
            (
                level
                for level in levels
                if level.stock_item_id == item_id and level.location_id == location.id
            ),
            None,
        )
        if (
            target is None
            or target.quantity - target.reserved_quantity - target.pending_out_quantity
            < quantity
        ):
            affected.append(
                {"stock_item_id": item_id, "required_base_quantity": quantity}
            )
        old_quantity = sum(
            (level.quantity for level in levels if level.stock_item_id == item_id),
            Decimal(0),
        )
        average = items[item_id].avg_cost_price
        remaining = old_quantity - quantity
        remaining_value = old_quantity * average - value
        prior = item_lines[0].invoice_snapshot
        subsequent = StockTransaction.objects.filter(
            stock_item_id=item_id,
            branch_id=branch,
            is_deleted=False,
        ).filter(Q(created_at__gt=receiving.completed_at))
        unchanged_basis = (
            not subsequent.filter(movement_type="PURCHASE_IN").exists()
            and old_quantity == Decimal(prior["previous_global_quantity"]) + quantity
            and average == Decimal(prior["new_average_cost_uzs"])
        )
        if unchanged_basis:
            new_average = Decimal(prior["previous_average_cost_uzs"])
        elif remaining < 0 or remaining_value < 0:
            affected.append(
                {
                    "stock_item_id": item_id,
                    "reason": "Cost basis cannot be reversed safely",
                }
            )
            new_average = Decimal(0)
        else:
            new_average = (
                (remaining_value / remaining).quantize(
                    Decimal(".0001"), rounding=ROUND_HALF_UP
                )
                if remaining
                else Decimal(0)
            )
        original_ids = {line.stock_transaction_id for line in item_lines}
        latest = (
            StockTransaction.objects.filter(
                stock_item_id=item_id,
                branch_id=branch,
                is_deleted=False,
                movement_type="PURCHASE_IN",
                reversal__isnull=True,
            )
            .exclude(pk__in=original_ids)
            .order_by("-created_at", "-id")
            .first()
        )
        groups[item_id] = {
            "average": new_average,
            "last": latest.unit_cost
            if latest
            else Decimal(prior["previous_last_cost_uzs"]),
        }
        for line in item_lines:
            original = line.stock_transaction
            if (
                original is None
                or original.is_deleted
                or original.location_id != location.id
                or original.base_quantity != line.base_quantity
                or original.total_cost != line.line_total_uzs
            ):
                affected.append(
                    {
                        "stock_item_id": item_id,
                        "reason": "Posting evidence is incomplete",
                    }
                )
                continue
            movement = StockTransaction.objects.filter(
                stock_item_id=item_id, branch_id=branch
            ).filter(
                Q(created_at__gt=original.created_at)
                | Q(created_at=original.created_at, id__gt=original.id),
            )
            if line.batch_created_id:
                batch = batches.get(line.batch_created_id)
                if (
                    batch is None
                    or batch.stock_item_id != item_id
                    or batch.current_quantity - batch.reserved_quantity
                    < line.base_quantity
                ):
                    affected.append(
                        {"stock_item_id": item_id, "batch_id": line.batch_created_id}
                    )
                movement = movement.filter(batch_id=line.batch_created_id)
            else:
                movement = movement.filter(location=location)
            # Replenishment after consumption is insufficient proof of the
            # original lot's availability. Deleted movements also break proof.
            if movement.filter(
                Q(quantity_after__lt=F("quantity_before")) | Q(is_deleted=True)
            ).exists():
                affected.append(
                    {
                        "stock_item_id": item_id,
                        "batch_id": line.batch_created_id,
                        "reason": "A later outbound or deleted movement exists",
                    }
                )
    if affected:
        _unavailable(affected)
    total = receiving.received_value_uzs
    if abs(supplier.current_balance - total) > MAX_INVOICE_VALUE:
        fail("VALIDATION_ERROR", "Reversed supplier balance exceeds capacity.")
    po_lines = {
        line.id: line
        for line in PurchaseOrderItem.objects.select_for_update()
        .filter(
            id__in=sorted({line.po_item_id for line in lines}),
            is_deleted=False,
        )
        .order_by("id")
    }
    correction = PurchaseReceivingCorrection.objects.create(
        receiving=receiving,
        reason=reason,
        requested_by=actor,
        reviewed_by=actor,
        reviewed_at=timezone.now(),
        review_note=reason,
        branch_id=branch,
    )
    movements = []
    for line in lines:
        result, status = StockLevelService.adjust(
            stock_item_id=line.stock_item_id,
            location_id=location.id,
            quantity=line.base_quantity,
            unit_id=line.base_unit_id,
            unit_cost=line.base_unit_cost,
            inventory_value=line.line_total_uzs,
            movement_type="RETURN_TO_SUPPLIER",
            user_id=actor.id,
            batch_id=line.batch_created_id,
            reference_type="PurchaseReceivingCorrection",
            reference_id=correction.id,
            branch_id=branch,
            action_id=uuid5(action, f"line:{line.id}"),
            idempotency_key=key,
            reversal_of_id=line.stock_transaction_id,
            notes=reason,
        )
        if status >= 400:
            raise InvoiceError((result, status))
        movements.append(result["data"]["transaction_id"])
        if line.batch_created_id:
            batch = batches[line.batch_created_id]
            batch.current_quantity -= line.base_quantity
            batch.status = "CONSUMED" if batch.current_quantity == 0 else batch.status
            batch.save(update_fields=["current_quantity", "status", "updated_at"])
        po_line = po_lines[line.po_item_id]
        po_line.quantity_received -= line.quantity_received
        po_line.quantity_canceled = po_line.quantity_ordered
        po_line.save(update_fields=["quantity_received", "quantity_canceled"])
    for item_id in item_ids:
        item = items[item_id]
        item.avg_cost_price, item.last_cost_price = (
            groups[item_id]["average"],
            groups[item_id]["last"],
        )
        item.save(update_fields=["avg_cost_price", "last_cost_price", "updated_at"])
    supplier_transaction = SupplierLedgerService.record_return(
        supplier.id,
        total,
        reference_type="PurchaseReceivingCorrection",
        reference_id=correction.id,
        performed_by=actor,
        note=reason,
        invoice_posting_id=action,
    )
    if supplier_transaction is None:
        fail("SUPPLIER_NOT_FOUND", "Supplier not found.", 404)
    correction.status = "APPROVED"
    correction.supplier_balance_before = supplier_transaction.balance_before
    correction.supplier_balance_after = supplier_transaction.balance_after
    correction.save(
        update_fields=["status", "supplier_balance_before", "supplier_balance_after"]
    )
    po.status = "CANCELED"
    po.save(update_fields=["status", "updated_at"])
    receiving.reversed_at, receiving.reversed_by, receiving.reversal_reason = (
        timezone.now(),
        actor,
        reason,
    )
    receiving.reversal_manifest = {
        "version": 1,
        "correction_id": correction.id,
        "reason": reason,
        "reversed_at": local_time(receiving.reversed_at),
        "reversed_by_id": actor.id,
        "stock_transaction_ids": movements,
        "supplier_transaction_id": supplier_transaction.id,
        "total_uzs": int(total),
        "supplier_balance_before_uzs": str(supplier_transaction.balance_before),
        "supplier_balance_after_uzs": str(supplier_transaction.balance_after),
    }
    receiving.save(
        update_fields=[
            "reversed_at",
            "reversed_by",
            "reversal_reason",
            "reversal_manifest",
            "updated_at",
        ]
    )
    refresh_supplier_prices(link_ids)
    AuditLog.objects.create(
        actor=actor,
        action="SUPPLIER_INVOICE_REVERSE",
        target_type="PurchaseReceiving",
        target_id=receiving.id,
        branch_id=branch,
        metadata={
            "manifest_version": 1,
            "action_id": str(action),
            "correction_id": correction.id,
            "reason": reason,
        },
    )
    return ServiceResponse.success(
        {"invoice": serialize(receiving, actor)}, "Supplier invoice reversed"
    )
