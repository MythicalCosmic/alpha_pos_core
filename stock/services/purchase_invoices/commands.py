"""One atomic command over internal purchase/receiving records."""

from datetime import timedelta

from django.db import IntegrityError

from base.helpers.response import ServiceResponse
from base.models import AuditLog
from stock.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceiving,
    PurchaseReceivingItem,
)
from stock.services.base_service import generate_number
from stock.services.purchase_service import PurchaseReceivingService
from stock.services.purchase_validation import purchase_datetime
from .idempotency import InvoiceError, execute
from .posting import finalize
from .serialization import local_time, serialize
from .validation import (
    actor_scope,
    known_price,
    lock_catalog,
    validate_header,
    validate_lines,
)


class DirectPurchaseInvoiceService:
    @classmethod
    def receive(cls, *, actor, payload, idempotency_key):
        try:
            branch = actor_scope(actor, "stock.purchase_invoice.receive")
            validate_header(payload)
            return execute(
                actor=actor,
                branch=branch,
                operation="receive",
                target="new",
                key=idempotency_key,
                payload=payload,
                command=lambda action, key: cls._receive(
                    actor, branch, payload, action, key
                ),
            )
        except InvoiceError as exc:
            return exc.result
        except IntegrityError as exc:
            # Named uniqueness is the final defense if another writer does
            # not take the supplier lock. Never report unrelated DB errors as
            # successful or fabricate a completed document.
            constraint = getattr(
                getattr(exc.__cause__, "diag", None), "constraint_name", ""
            )
            if constraint == "uniq_direct_supplier_invoice_number":
                return ServiceResponse.conflict(
                    "DUPLICATE_SUPPLIER_INVOICE",
                    "This supplier invoice number is already recorded.",
                )
            raise

    @classmethod
    def _receive(cls, actor, branch, payload, action, key):
        header = validate_header(payload)
        catalog = lock_catalog(header, branch)
        supplier, location, _, _, _, _, replacement = catalog
        lines, total = validate_lines(header, catalog)
        po = PurchaseOrder.objects.create(
            source_type="DIRECT_INVOICE",
            order_number=generate_number("DPO", PurchaseOrder, "order_number"),
            supplier=supplier,
            delivery_location=location,
            status="CONFIRMED",
            order_date=header["invoice_date"],
            subtotal=total,
            total=total,
            currency="UZS",
            payment_due_date=purchase_datetime(
                header["invoice_date"] + timedelta(days=supplier.payment_terms_days),
                "payment_due_date",
            ),
            created_by=actor,
            notes=header["notes"],
            branch_id=branch,
        )
        receiving = PurchaseReceiving.objects.create(
            source_type="DIRECT_INVOICE",
            receiving_number=generate_number(
                "RCV", PurchaseReceiving, "receiving_number"
            ),
            purchase_order=po,
            supplier=supplier,
            location=location,
            received_date=header["invoice_date"],
            invoice_date=header["invoice_date"],
            supplier_invoice_number=header["supplier_invoice_number"],
            supplier_invoice_number_normalized=header["normalized_number"],
            received_by=actor,
            notes=header["notes"],
            branch_id=branch,
            replaces=replacement,
        )
        for line in lines:
            link, item, unit = line["link"], line["item"], line["unit"]
            po_line = PurchaseOrderItem.objects.create(
                purchase_order=po,
                supplier_stock_item=link,
                stock_item=item,
                quantity_ordered=line["quantity"],
                unit=unit,
                unit_price=line["price"],
                total_price=line["total"],
                notes=line["notes"],
                branch_id=branch,
            )
            PurchaseReceivingItem.objects.create(
                receiving=receiving,
                po_item=po_line,
                supplier_stock_item=link,
                stock_item=item,
                quantity_received=line["quantity"],
                unit=unit,
                unit_cost=line["price"],
                line_total_uzs=line["total"],
                is_free=line["is_free"],
                free_reason=line["free_reason"],
                batch_number=line["batch_number"],
                expiry_date=line["expiry_date"],
                quality_status="PASSED",
                notes=line["notes"],
                branch_id=branch,
                invoice_snapshot={
                    "version": 1,
                    "stock_item_name": item.name,
                    "purchase_quantity": str(line["quantity"]),
                    "purchase_unit": unit.short_name,
                    "purchase_unit_id": unit.id,
                    "purchase_unit_price_uzs": str(line["price"]),
                    "base_unit": item.base_unit.short_name,
                    "base_unit_id": item.base_unit_id,
                    "line_total_uzs": str(line["total"]),
                    "price_change_confirmed": line["price_change_confirmed"],
                    "price_change_reason": line["price_change_reason"],
                    "supplier_price_before": {
                        "price": str(link.price),
                        "known": known_price(link),
                        "currency": link.currency,
                        "source": link.price_source,
                        "last_price_update": local_time(link.last_price_update),
                    },
                },
            )
        result, status = PurchaseReceivingService.complete(
            receiving.id,
            actor=actor,
            action_id=action,
            idempotency_key=key,
            _direct_invoice=True,
        )
        if status >= 400:
            raise InvoiceError((result, status))
        receiving = finalize(receiving, actor)
        AuditLog.objects.create(
            actor=actor,
            action="SUPPLIER_INVOICE_RECEIVE",
            target_type="PurchaseReceiving",
            target_id=receiving.id,
            branch_id=branch,
            metadata={
                "manifest_version": 1,
                "action_id": str(action),
                "total_uzs": int(total),
            },
        )
        return ServiceResponse.created(
            {"invoice": serialize(receiving, actor)}, "Supplier invoice posted"
        )

    @classmethod
    def reverse(cls, *, actor, invoice_id, payload, idempotency_key):
        from .reversal import reverse

        return reverse(
            actor=actor,
            invoice_id=invoice_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
