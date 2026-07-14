from typing import Dict, Any, List, Tuple
from decimal import Decimal
from datetime import date, timedelta
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseReceiving, PurchaseReceivingItem,
    SupplierStockItem, StockBatch, StockSettings
)
from stock.services.base_service import to_decimal, generate_number
from stock.repositories import (
    PurchaseOrderRepository, PurchaseOrderItemRepository,
    PurchaseReceivingRepository, PurchaseReceivingItemRepository,
    SupplierRepository, StockItemRepository, StockLocationRepository,
    StockUnitRepository,
)


def _pagination_data(page_obj, paginator):
    return {
        "page": page_obj.number,
        "per_page": paginator.per_page,
        "total": paginator.count,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
        "has_previous": page_obj.has_previous(),
    }


class PurchaseOrderService:

    @classmethod
    def serialize(cls, po: PurchaseOrder,
                  include_items: bool = True,
                  include_receivings: bool = False) -> Dict[str, Any]:
        data = {
            "id": po.id,
            "uuid": str(po.uuid),
            "order_number": po.order_number,

            "supplier_id": po.supplier_id,
            "supplier": {
                "id": po.supplier.id,
                "name": po.supplier.name,
                "code": po.supplier.code,
            },

            "delivery_location_id": po.delivery_location_id,
            "delivery_location": po.delivery_location.name,

            "status": po.status,
            "status_display": po.get_status_display(),
            "payment_status": po.payment_status,
            "payment_status_display": po.get_payment_status_display(),

            "order_date": po.order_date.isoformat(),
            "expected_date": po.expected_date.isoformat() if po.expected_date else None,
            "received_date": po.received_date.isoformat() if po.received_date else None,
            "payment_due_date": po.payment_due_date.isoformat() if po.payment_due_date else None,

            "subtotal": str(po.subtotal),
            "tax_amount": str(po.tax_amount),
            "shipping_cost": str(po.shipping_cost),
            "discount": str(po.discount),
            "total": str(po.total),
            "currency": po.currency,

            "created_by_id": po.created_by_id,
            "approved_by_id": po.approved_by_id,

            "notes": po.notes,
            "created_at": po.created_at.isoformat(),
            "updated_at": po.updated_at.isoformat(),
        }

        if include_items:
            data["items"] = [
                PurchaseOrderItemService.serialize(item)
                for item in po.items.select_related("stock_item", "unit")
            ]
            data["item_count"] = len(data["items"])

        if include_receivings:
            data["receivings"] = [
                PurchaseReceivingService.serialize_brief(rcv)
                for rcv in po.receivings.all()
            ]

        return data

    @classmethod
    def serialize_brief(cls, po: PurchaseOrder) -> Dict[str, Any]:
        return {
            "id": po.id,
            "order_number": po.order_number,
            "supplier_name": po.supplier.name,
            "status": po.status,
            "status_display": po.get_status_display(),
            "order_date": po.order_date.isoformat(),
            "total": str(po.total),
            "currency": po.currency,
        }

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             search: str = None,
             supplier_id: int = None,
             status: str = None,
             payment_status: str = None,
             date_from: date = None,
             date_to: date = None,
             location_id: int = None) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.select_related("supplier", "delivery_location")

        if search:
            queryset = queryset.filter(
                Q(order_number__icontains=search) |
                Q(supplier__name__icontains=search)
            )

        if supplier_id:
            queryset = queryset.filter(supplier_id=supplier_id)

        if status:
            queryset = queryset.filter(status=status)

        if payment_status:
            queryset = queryset.filter(payment_status=payment_status)

        if date_from:
            queryset = queryset.filter(order_date__gte=date_from)

        if date_to:
            queryset = queryset.filter(order_date__lte=date_to)

        if location_id:
            queryset = queryset.filter(delivery_location_id=location_id)

        queryset = queryset.order_by("-order_date", "-created_at")

        page_obj, paginator = PurchaseOrderRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "orders": [cls.serialize_brief(po) for po in page_obj],
            "pagination": _pagination_data(page_obj, paginator),
            "statuses": [{"value": c[0], "label": c[1]} for c in PurchaseOrder.Status.choices],
            "payment_statuses": [{"value": c[0], "label": c[1]} for c in PurchaseOrder.PaymentStatus.choices],
        })

    @classmethod
    def get_pending(cls, supplier_id: int = None) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.filter(
            status__in=["DRAFT", "SENT", "CONFIRMED", "PARTIAL"]
        ).select_related("supplier", "delivery_location")

        if supplier_id:
            queryset = queryset.filter(supplier_id=supplier_id)

        orders = queryset.order_by("expected_date", "order_date")

        return ServiceResponse.success(data={
            "orders": [cls.serialize_brief(po) for po in orders],
            "count": orders.count()
        })

    @classmethod
    def get(cls, po_id: int,
            include_receivings: bool = True) -> Tuple[Dict[str, Any], int]:
        po = PurchaseOrderRepository.get_with_relations(po_id)

        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        return ServiceResponse.success(data={
            "order": cls.serialize(po, include_receivings=include_receivings)
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               supplier_id: int,
               delivery_location_id: int,
               order_date: date,
               created_by_id: int,
               expected_date: date = None,
               currency: str = "UZS",
               shipping_cost: Decimal = Decimal("0"),
               discount: Decimal = Decimal("0"),
               notes: str = "",
               items: List[Dict] = None) -> Tuple[Dict[str, Any], int]:

        supplier = SupplierRepository.first(id=supplier_id, is_active=True)
        if not supplier:
            return ServiceResponse.not_found("Supplier not found")

        location = StockLocationRepository.first(id=delivery_location_id, is_active=True)
        if not location:
            return ServiceResponse.not_found("Delivery location not found")

        order_number = generate_number("PO", PurchaseOrder, "order_number")

        # Apply the supplier's terms: payment is due payment_terms_days after the
        # order, and expected delivery is lead_time_days out. Previously both
        # were just set to order_date with no offset, so payables aging and the
        # delivery forecast (which the AI assistant reads) were always wrong.
        payment_due_date = None
        if supplier.payment_terms_days:
            payment_due_date = order_date + timedelta(days=supplier.payment_terms_days)

        if not expected_date and supplier.lead_time_days:
            expected_date = order_date + timedelta(days=supplier.lead_time_days)

        po = PurchaseOrderRepository.create(
            order_number=order_number,
            supplier=supplier,
            delivery_location=location,
            status=PurchaseOrder.Status.DRAFT,
            order_date=order_date,
            expected_date=expected_date,
            currency=currency,
            shipping_cost=to_decimal(shipping_cost),
            discount=to_decimal(discount),
            payment_due_date=payment_due_date,
            created_by_id=created_by_id,
            notes=notes,
        )

        if items:
            for item_data in items:
                result, status = PurchaseOrderItemService.add(
                    purchase_order_id=po.id,
                    stock_item_id=item_data["stock_item_id"],
                    quantity=item_data["quantity"],
                    unit_id=item_data["unit_id"],
                    unit_price=item_data["unit_price"],
                    discount_percent=item_data.get("discount_percent", 0),
                    tax_percent=item_data.get("tax_percent", 0),
                    notes=item_data.get("notes", ""),
                )
                if status >= 400:
                    return result, status

        cls._recalculate_totals(po.id)
        po.refresh_from_db()

        return ServiceResponse.success(data={
            "id": po.id,
            "order_number": po.order_number,
            "order": cls.serialize(po)
        }, message=f"Purchase order {order_number} created")

    @classmethod
    @transaction.atomic
    def create_from_low_stock(cls,
                              supplier_id: int,
                              delivery_location_id: int,
                              created_by_id: int,
                              reorder_quantity_multiplier: Decimal = Decimal("1")) -> Tuple[Dict[str, Any], int]:

        supplier_items = SupplierStockItem.objects.filter(
            supplier_id=supplier_id,
            supplier__is_active=True
        ).select_related("stock_item", "unit")

        items_to_order = []

        for si in supplier_items:
            from .level_service import StockLevelService
            available = StockLevelService.get_available(si.stock_item_id)

            if available < si.stock_item.reorder_point:
                shortage = si.stock_item.reorder_point - available
                order_qty = max(shortage * reorder_quantity_multiplier, si.min_order_qty)

                if si.pack_size > 1:
                    packs_needed = (order_qty / si.pack_size).quantize(Decimal("1"), rounding="ROUND_UP")
                    order_qty = packs_needed * si.pack_size

                items_to_order.append({
                    "stock_item_id": si.stock_item_id,
                    "quantity": order_qty,
                    "unit_id": si.unit_id,
                    "unit_price": si.price,
                })

        if not items_to_order:
            return ServiceResponse.success(data={
                "created": False,
                "reason": "No items below reorder point for this supplier"
            })

        return cls.create(
            supplier_id=supplier_id,
            delivery_location_id=delivery_location_id,
            order_date=timezone.localdate(),
            created_by_id=created_by_id,
            items=items_to_order,
            notes="Auto-generated from low stock"
        )

    @classmethod
    @transaction.atomic
    def update(cls, po_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        po = PurchaseOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        if po.status != PurchaseOrder.Status.DRAFT:
            return ServiceResponse.error("Can only update orders in DRAFT status")

        update_fields = ["updated_at"]

        if "supplier_id" in kwargs:
            supplier = SupplierRepository.first(id=kwargs["supplier_id"], is_active=True)
            if not supplier:
                return ServiceResponse.not_found("Supplier not found")
            po.supplier = supplier
            update_fields.append("supplier")

        if "delivery_location_id" in kwargs:
            location = StockLocationRepository.first(id=kwargs["delivery_location_id"], is_active=True)
            if not location:
                return ServiceResponse.not_found("Delivery location not found")
            po.delivery_location = location
            update_fields.append("delivery_location")

        for field in ["order_date", "expected_date", "currency", "shipping_cost",
                      "discount", "payment_due_date", "notes"]:
            if field in kwargs:
                value = kwargs[field]
                if field in ["shipping_cost", "discount"]:
                    value = to_decimal(value)
                setattr(po, field, value)
                update_fields.append(field)

        po.save(update_fields=update_fields)

        if "shipping_cost" in kwargs or "discount" in kwargs:
            cls._recalculate_totals(po_id)
            po.refresh_from_db()

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Purchase order updated")

    @classmethod
    def _recalculate_totals(cls, po_id: int):
        po = PurchaseOrderRepository.get_by_id(po_id)
        if not po:
            return

        items = po.items.all()

        subtotal = sum(item.total_price for item in items)
        tax_amount = sum(
            item.total_price * item.tax_percent / 100
            for item in items
        )

        po.subtotal = subtotal
        po.tax_amount = tax_amount

        # The PO-level discount is applied on top of any per-line discounts
        # (already baked into total_price). Clamp it so it can never exceed the
        # gross (subtotal + tax + shipping), which would otherwise produce a
        # negative total — mirroring how order totals are floored at zero.
        gross = subtotal + tax_amount + po.shipping_cost
        if po.discount > gross:
            po.discount = gross
        po.total = gross - po.discount
        po.save(update_fields=["subtotal", "tax_amount", "discount", "total", "updated_at"])

    @classmethod
    @transaction.atomic
    def send(cls, po_id: int) -> Tuple[Dict[str, Any], int]:
        po = PurchaseOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        if po.status != PurchaseOrder.Status.DRAFT:
            return ServiceResponse.error(f"Cannot send order in {po.status} status")

        if not po.items.exists():
            return ServiceResponse.error("Cannot send order with no items")

        po.status = PurchaseOrder.Status.SENT
        po.save(update_fields=["status", "updated_at"])

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Purchase order sent to supplier")

    @classmethod
    @transaction.atomic
    def confirm(cls, po_id: int, approved_by_id: int = None) -> Tuple[Dict[str, Any], int]:
        po = PurchaseOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        if po.status != PurchaseOrder.Status.SENT:
            return ServiceResponse.error(f"Cannot confirm order in {po.status} status")

        settings = StockSettings.load()
        if settings.require_po_approval and not approved_by_id:
            return ServiceResponse.error("PO approval is required")

        po.status = PurchaseOrder.Status.CONFIRMED
        if approved_by_id:
            po.approved_by_id = approved_by_id
        po.save(update_fields=["status", "approved_by", "updated_at"])

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Purchase order confirmed")

    @classmethod
    @transaction.atomic
    def cancel(cls, po_id: int, reason: str = "") -> Tuple[Dict[str, Any], int]:
        po = PurchaseOrderRepository.get_by_id(po_id)
        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        if po.status in [PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELED]:
            return ServiceResponse.error(f"Cannot cancel order in {po.status} status")

        if po.receivings.filter(status=PurchaseReceiving.Status.COMPLETED).exists():
            return ServiceResponse.error("Cannot cancel order with completed receivings")

        po.status = PurchaseOrder.Status.CANCELED
        if reason:
            po.notes = f"{po.notes}\nCancelled: {reason}".strip()
        po.save(update_fields=["status", "notes", "updated_at"])

        return ServiceResponse.success(data={
            "order": cls.serialize(po)
        }, message="Purchase order cancelled")

    @classmethod
    @transaction.atomic
    def record_payment(cls, po_id: int,
                       amount: Decimal,
                       payment_date: date = None,
                       notes: str = "") -> Tuple[Dict[str, Any], int]:
        # Lock the PO so concurrent payments accumulate correctly and can't
        # both over-reduce the supplier balance.
        try:
            po = PurchaseOrder.objects.select_for_update().get(pk=po_id)
        except PurchaseOrder.DoesNotExist:
            return ServiceResponse.not_found("Purchase order not found")

        amount = to_decimal(amount)
        if amount <= 0:
            return ServiceResponse.validation_error(
                errors={"amount": "Must be greater than 0"},
                message="Payment amount must be greater than 0",
            )

        # Track cumulative payments so partial payments settle to PAID and a
        # PO can't be paid past its total (which would over-credit the supplier).
        remaining = po.total - po.amount_paid
        if remaining <= 0:
            return ServiceResponse.error("Purchase order is already fully paid")
        if amount > remaining:
            return ServiceResponse.error(
                f"Payment {amount} exceeds the remaining balance {remaining}"
            )

        # The supplier balance is an audited ledger, not a derived PO field.
        # Posting through the row-locked ledger prevents two payments on
        # different POs for the same supplier from overwriting one another.
        # This legacy API has no funding-account argument, so it deliberately
        # records no guessed SAFE/BANK/DRAWER treasury movement.
        from .supplier_ledger_service import SupplierLedgerService
        supplier_txn = SupplierLedgerService.record_purchase_order_payment(
            po.supplier_id, amount, po.id, note=notes,
        )
        if supplier_txn is None:
            transaction.set_rollback(True)
            return ServiceResponse.not_found("Supplier not found")

        po.amount_paid = po.amount_paid + amount
        if po.amount_paid >= po.total:
            po.payment_status = PurchaseOrder.PaymentStatus.PAID
        elif po.amount_paid > 0:
            po.payment_status = PurchaseOrder.PaymentStatus.PARTIAL
        else:
            po.payment_status = PurchaseOrder.PaymentStatus.UNPAID

        if notes:
            po.notes = f"{po.notes}\nPayment recorded: {amount} on {payment_date or timezone.localdate()}".strip()

        po.save(update_fields=["amount_paid", "payment_status", "notes", "updated_at"])

        return ServiceResponse.success(data={
            "payment_status": po.payment_status,
            "payment_status_display": po.get_payment_status_display(),
            "amount_paid": str(po.amount_paid),
            "remaining": str(po.total - po.amount_paid),
        }, message="Payment recorded")

    @classmethod
    def get_stats(cls, date_from: date = None, date_to: date = None) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.all()

        if date_from:
            queryset = queryset.filter(order_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(order_date__lte=date_to)

        by_status = {}
        for status in PurchaseOrder.Status.choices:
            by_status[status[0]] = queryset.filter(status=status[0]).count()

        total_value = queryset.exclude(
            status=PurchaseOrder.Status.CANCELED
        ).aggregate(total=Sum("total"))["total"] or Decimal("0")

        pending_value = queryset.filter(
            status__in=["DRAFT", "SENT", "CONFIRMED", "PARTIAL"]
        ).aggregate(total=Sum("total"))["total"] or Decimal("0")

        return ServiceResponse.success(data={
            "total_orders": queryset.count(),
            "by_status": by_status,
            "total_value": str(total_value),
            "pending_value": str(pending_value),
        })


class PurchaseOrderItemService:

    @classmethod
    def serialize(cls, item: PurchaseOrderItem) -> Dict[str, Any]:
        return {
            "id": item.id,
            "uuid": str(item.uuid),
            "purchase_order_id": item.purchase_order_id,
            "stock_item_id": item.stock_item_id,
            "stock_item": {
                "id": item.stock_item.id,
                "name": item.stock_item.name,
                "sku": item.stock_item.sku,
            },
            "quantity_ordered": str(item.quantity_ordered),
            "quantity_received": str(item.quantity_received),
            "quantity_pending": str(item.quantity_ordered - item.quantity_received),
            "unit": item.unit.short_name,
            "unit_price": str(item.unit_price),
            "discount_percent": str(item.discount_percent),
            "tax_percent": str(item.tax_percent),
            "total_price": str(item.total_price),
            "notes": item.notes,
        }

    @classmethod
    @transaction.atomic
    def add(cls,
            purchase_order_id: int,
            stock_item_id: int,
            quantity: Decimal,
            unit_id: int,
            unit_price: Decimal,
            discount_percent: Decimal = Decimal("0"),
            tax_percent: Decimal = Decimal("0"),
            supplier_stock_item_id: int = None,
            notes: str = "") -> Tuple[Dict[str, Any], int]:

        po = PurchaseOrderRepository.get_by_id(purchase_order_id)
        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        if po.status != PurchaseOrder.Status.DRAFT:
            return ServiceResponse.error("Can only add items to DRAFT orders")

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found("Stock item not found")

        unit = StockUnitRepository.first(id=unit_id, is_active=True)
        if not unit:
            return ServiceResponse.not_found("Unit not found")

        quantity = to_decimal(quantity)
        unit_price = to_decimal(unit_price)
        discount_percent = to_decimal(discount_percent)
        tax_percent = to_decimal(tax_percent)

        subtotal = quantity * unit_price
        discount_amount = subtotal * discount_percent / 100
        total_price = subtotal - discount_amount

        item = PurchaseOrderItemRepository.create(
            purchase_order=po,
            stock_item=stock_item,
            supplier_stock_item_id=supplier_stock_item_id,
            quantity_ordered=quantity,
            unit=unit,
            unit_price=unit_price,
            discount_percent=discount_percent,
            tax_percent=tax_percent,
            total_price=total_price,
            notes=notes,
        )

        PurchaseOrderService._recalculate_totals(purchase_order_id)

        return ServiceResponse.success(data={
            "id": item.id,
            "item": cls.serialize(item)
        }, message="Item added to order")

    @classmethod
    @transaction.atomic
    def update_item(cls, item_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        item = PurchaseOrderItemRepository.first(id=item_id)
        if not item:
            return ServiceResponse.not_found("Order item not found")

        # Need select_related for status check
        item = PurchaseOrderItem.objects.select_related("purchase_order").get(id=item_id)

        if item.purchase_order.status != PurchaseOrder.Status.DRAFT:
            return ServiceResponse.error("Can only update items in DRAFT orders")

        for field in ["quantity_ordered", "unit_price", "discount_percent", "tax_percent", "notes"]:
            if field in kwargs:
                value = kwargs[field]
                if field in ["quantity_ordered", "unit_price", "discount_percent", "tax_percent"]:
                    value = to_decimal(value)
                setattr(item, field, value)

        subtotal = item.quantity_ordered * item.unit_price
        discount_amount = subtotal * item.discount_percent / 100
        item.total_price = subtotal - discount_amount

        item.save()

        PurchaseOrderService._recalculate_totals(item.purchase_order_id)

        return ServiceResponse.success(data={
            "item": cls.serialize(item)
        }, message="Item updated")

    @classmethod
    @transaction.atomic
    def remove(cls, item_id: int) -> Tuple[Dict[str, Any], int]:
        item = PurchaseOrderItemRepository.first(id=item_id)
        if not item:
            return ServiceResponse.not_found("Order item not found")

        # Need select_related for status check
        item = PurchaseOrderItem.objects.select_related("purchase_order").get(id=item_id)

        if item.purchase_order.status != PurchaseOrder.Status.DRAFT:
            return ServiceResponse.error("Can only remove items from DRAFT orders")

        po_id = item.purchase_order_id
        item.delete()

        PurchaseOrderService._recalculate_totals(po_id)

        return ServiceResponse.success(message="Item removed")


class PurchaseReceivingService:

    @classmethod
    def serialize(cls, rcv: PurchaseReceiving,
                  include_items: bool = True) -> Dict[str, Any]:
        data = {
            "id": rcv.id,
            "uuid": str(rcv.uuid),
            "receiving_number": rcv.receiving_number,
            "purchase_order_id": rcv.purchase_order_id,
            "purchase_order_number": rcv.purchase_order.order_number,
            "location_id": rcv.location_id,
            "location_name": rcv.location.name,
            "received_date": rcv.received_date.isoformat(),
            "received_by_id": rcv.received_by_id,
            "status": rcv.status,
            "status_display": rcv.get_status_display(),
            "notes": rcv.notes,
            "created_at": rcv.created_at.isoformat(),
        }

        if include_items:
            data["items"] = [
                PurchaseReceivingItemService.serialize(item)
                for item in rcv.items.select_related("stock_item", "unit")
            ]

        return data

    @classmethod
    def serialize_brief(cls, rcv: PurchaseReceiving) -> Dict[str, Any]:
        return {
            "id": rcv.id,
            "receiving_number": rcv.receiving_number,
            "received_date": rcv.received_date.isoformat(),
            "status": rcv.status,
        }

    @classmethod
    @transaction.atomic
    def create(cls,
               purchase_order_id: int,
               received_by_id: int,
               location_id: int = None,
               received_date: date = None,
               notes: str = "") -> Tuple[Dict[str, Any], int]:

        po = PurchaseOrderRepository.get_by_id(purchase_order_id)
        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        if po.status not in [PurchaseOrder.Status.CONFIRMED, PurchaseOrder.Status.PARTIAL]:
            return ServiceResponse.error(f"Cannot receive order in {po.status} status")

        location_id = location_id or po.delivery_location_id

        location = StockLocationRepository.first(id=location_id, is_active=True)
        if not location:
            return ServiceResponse.not_found("Location not found")

        receiving_number = generate_number("RCV", PurchaseReceiving, "receiving_number")

        rcv = PurchaseReceivingRepository.create(
            receiving_number=receiving_number,
            purchase_order=po,
            location=location,
            received_date=received_date or timezone.localdate(),
            received_by_id=received_by_id,
            status=PurchaseReceiving.Status.DRAFT,
            notes=notes,
        )

        return ServiceResponse.success(data={
            "id": rcv.id,
            "receiving_number": receiving_number,
            "receiving": cls.serialize(rcv)
        }, message=f"Receiving {receiving_number} created")

    @classmethod
    @transaction.atomic
    def add_item(cls, receiving_id: int, po_item_id: int, quantity_received,
                 batch_number: str = "", expiry_date: date = None,
                 unit_cost=None, quality_status: str = "PASSED",
                 notes: str = "") -> Tuple[Dict[str, Any], int]:
        # Delegate to the validated PurchaseReceivingItemService.add. The
        # previous bespoke body trusted raw input — it accepted negative /
        # over-pending quantities and float costs straight from the client,
        # driving PurchaseOrderItem.quantity_received negative and poisoning
        # the moving-average cost on complete(). The defaults here also stop a
        # request that omits an optional field from 500-ing with a TypeError.
        return PurchaseReceivingItemService.add(
            receiving_id=receiving_id,
            po_item_id=po_item_id,
            quantity_received=quantity_received,
            batch_number=batch_number or "",
            expiry_date=expiry_date,
            unit_cost=unit_cost,
            quality_status=quality_status or "PASSED",
            notes=notes or "",
        )

    @classmethod
    @transaction.atomic
    def complete(cls, receiving_id: int) -> Tuple[Dict[str, Any], int]:
        # Lock the receiving row and re-check status under the lock. Without the
        # lock two concurrent complete() calls both read DRAFT and both run the
        # stock-in + cost-update loop, doubling received stock and corrupting the
        # average cost. select_for_update serializes them; the loser sees the
        # status already flipped to COMPLETED and bails out below.
        rcv = PurchaseReceivingRepository.get_for_update(receiving_id)
        if not rcv:
            return ServiceResponse.not_found("Receiving not found")

        if rcv.status != PurchaseReceiving.Status.DRAFT:
            return ServiceResponse.error("Receiving already completed")

        if not rcv.items.exists():
            return ServiceResponse.error("No items in receiving")

        settings = StockSettings.load()
        po = rcv.purchase_order
        received_value = Decimal("0")

        for item in rcv.items.select_related("stock_item", "unit", "po_item"):
            received_value += to_decimal(item.unit_cost) * to_decimal(item.quantity_received)
            batch = None
            if settings.track_batches or item.stock_item.track_batches:
                from .batch_service import StockBatchService
                batch_result, batch_status = StockBatchService.create(
                    stock_item_id=item.stock_item_id,
                    location_id=rcv.location_id,
                    quantity=item.quantity_received,
                    unit_cost=item.unit_cost,
                    batch_number=item.batch_number or None,
                    expiry_date=item.expiry_date,
                    supplier_id=po.supplier_id,
                    purchase_order_id=po.id,
                    quality_status=item.quality_status,
                )
                if batch_status >= 400:
                    # Returning from an @atomic method commits unless rollback
                    # is explicitly requested. Earlier receiving items may
                    # already have changed batches/levels, so an error here
                    # must unwind the whole receiving, not leave a retryable
                    # DRAFT with partially received stock.
                    transaction.set_rollback(True)
                    return batch_result, batch_status
                batch = StockBatch.objects.get(id=batch_result["data"]["id"])
                item.batch_created = batch
                item.save(update_fields=["batch_created"])

            from .level_service import StockLevelService
            level_result, level_status = StockLevelService.adjust(
                stock_item_id=item.stock_item_id,
                location_id=rcv.location_id,
                quantity=item.quantity_received,
                movement_type="PURCHASE_IN",
                user_id=rcv.received_by_id,
                unit_id=item.unit_id,
                batch_id=batch.id if batch else None,
                reference_type="PurchaseReceiving",
                reference_id=rcv.id,
                unit_cost=item.unit_cost,
                notes=f"PO: {po.order_number}",
            )
            if level_status >= 400:
                transaction.set_rollback(True)
                return level_result, level_status

            # Serialize receipts that target the same PO line. A prior
            # F-expression followed by an unlocked reload/save made the sync
            # bookkeeping visible, but reintroduced a lost-update window: a
            # second F increment could land between that reload and save and be
            # overwritten. Lock the row first, increment the current value, and
            # use one SyncMixin.save() so both the quantity and sync version are
            # published atomically.
            PurchaseOrderItem = item.po_item.__class__
            po_item = PurchaseOrderItem.objects.select_for_update().get(
                pk=item.po_item_id,
            )
            po_item.quantity_received += item.quantity_received
            po_item.save(update_fields=['quantity_received'])

            from .item_service import StockItemService
            cost_result, cost_status = StockItemService.update_cost(
                item.stock_item_id, item.unit_cost, "AVG",
                received_qty=item.quantity_received,
            )
            if cost_status >= 400:
                transaction.set_rollback(True)
                return cost_result, cost_status

        rcv.status = PurchaseReceiving.Status.COMPLETED
        rcv.save(update_fields=["status", "updated_at"])

        cls._update_po_status(po)

        # Record the supplier debt: receiving goods worth `received_value` means
        # we now owe the supplier that much (a PURCHASE ledger row). Previously
        # the debt was never recorded — the money owed vanished.
        if received_value > 0 and po.supplier_id:
            from .supplier_ledger_service import SupplierLedgerService
            SupplierLedgerService.record_purchase(
                po.supplier_id, received_value,
                reference_type="PurchaseReceiving", reference_id=rcv.id,
                performed_by=rcv.received_by,
            )

        return ServiceResponse.success(data={
            "receiving": cls.serialize(rcv)
        }, message="Receiving completed")

    @classmethod
    @transaction.atomic
    def update_item(cls, item_id: int, quantity_received: Decimal = None,
                    batch_number: str = None, expiry_date: date = None,
                    unit_cost: Decimal = None, quality_status: str = None,
                    notes: str = None) -> Tuple[Dict[str, Any], int]:
        item = PurchaseReceivingItemRepository.first(id=item_id)
        if not item:
            return ServiceResponse.not_found("Receiving item not found")

        # Need select_related for status check
        item = PurchaseReceivingItem.objects.select_related("receiving", "po_item").get(id=item_id)

        if item.receiving.status != PurchaseReceiving.Status.DRAFT:
            return ServiceResponse.error("Cannot update items in completed receiving")

        if quantity_received is not None:
            quantity_received = to_decimal(quantity_received)

            # Mirror add()'s guard: reject non-positive or over-pending
            # quantities. Without this an update can drive
            # PurchaseOrderItem.quantity_received negative on complete() and
            # poison the moving-average cost.
            if quantity_received <= 0:
                return ServiceResponse.validation_error(
                    errors={"quantity_received": "Must be greater than 0"},
                )

            pending = item.po_item.quantity_ordered - item.po_item.quantity_received
            if quantity_received > pending:
                return ServiceResponse.validation_error(
                    errors={"quantity_received": f"Cannot receive more than pending quantity ({pending})"}
                )

            item.quantity_received = quantity_received

        if batch_number is not None:
            item.batch_number = batch_number

        if expiry_date is not None:
            item.expiry_date = expiry_date

        if unit_cost is not None:
            item.unit_cost = to_decimal(unit_cost)

        if quality_status is not None:
            item.quality_status = quality_status

        if notes is not None:
            item.notes = notes

        item.save()

        return ServiceResponse.success(data={
            "item": PurchaseReceivingItemService.serialize(item)
        }, message="Receiving item updated")

    @classmethod
    def _update_po_status(cls, po: PurchaseOrder):
        items = po.items.all()

        fully_received = all(
            item.quantity_received >= item.quantity_ordered
            for item in items
        )
        partially_received = any(
            item.quantity_received > 0
            for item in items
        )

        if fully_received:
            po.status = PurchaseOrder.Status.RECEIVED
            po.received_date = timezone.localdate()
        elif partially_received:
            po.status = PurchaseOrder.Status.PARTIAL

        po.save(update_fields=["status", "received_date", "updated_at"])


class PurchaseReceivingItemService:

    @classmethod
    def serialize(cls, item: PurchaseReceivingItem) -> Dict[str, Any]:
        return {
            "id": item.id,
            "uuid": str(item.uuid),
            "receiving_id": item.receiving_id,
            "po_item_id": item.po_item_id,
            "stock_item_id": item.stock_item_id,
            "stock_item_name": item.stock_item.name,
            "quantity_received": str(item.quantity_received),
            "unit": item.unit.short_name,
            "batch_number": item.batch_number,
            "expiry_date": item.expiry_date.isoformat() if item.expiry_date else None,
            "unit_cost": str(item.unit_cost),
            "quality_status": item.quality_status,
            "notes": item.notes,
            "batch_created_id": item.batch_created_id,
        }

    @classmethod
    @transaction.atomic
    def add(cls,
            receiving_id: int,
            po_item_id: int,
            quantity_received: Decimal,
            batch_number: str = "",
            expiry_date: date = None,
            unit_cost: Decimal = None,
            quality_status: str = "PASSED",
            notes: str = "") -> Tuple[Dict[str, Any], int]:

        rcv = PurchaseReceivingRepository.get_by_id(receiving_id)
        if not rcv:
            return ServiceResponse.not_found("Receiving not found")

        if rcv.status != PurchaseReceiving.Status.DRAFT:
            return ServiceResponse.error("Cannot add items to completed receiving")

        po_item = PurchaseOrderItemRepository.first(
            id=po_item_id,
            purchase_order=rcv.purchase_order
        )
        if not po_item:
            return ServiceResponse.not_found("PO item not found")

        # Ensure select_related for stock_item and unit
        po_item = PurchaseOrderItem.objects.select_related("stock_item", "unit").get(id=po_item.id)

        quantity_received = to_decimal(quantity_received)

        # Reject non-positive quantities. Without this, a negative value
        # corrupts PO totals (quantity_received goes negative) and feeds
        # negative unit_cost * quantity into the moving-average cost.
        if quantity_received <= 0:
            return ServiceResponse.validation_error(
                errors={"quantity_received": "Must be greater than 0"},
            )

        already_received = po_item.quantity_received
        pending = po_item.quantity_ordered - already_received

        if quantity_received > pending:
            return ServiceResponse.validation_error(
                errors={"quantity_received": f"Cannot receive more than pending quantity ({pending})"}
            )

        item = PurchaseReceivingItemRepository.create(
            receiving=rcv,
            po_item=po_item,
            stock_item=po_item.stock_item,
            quantity_received=quantity_received,
            unit=po_item.unit,
            batch_number=batch_number,
            expiry_date=expiry_date,
            unit_cost=unit_cost or po_item.unit_price,
            quality_status=quality_status,
            notes=notes,
        )

        return ServiceResponse.success(data={
            "id": item.id,
            "item": cls.serialize(item)
        }, message="Item added to receiving")

    @classmethod
    @transaction.atomic
    def add_all_pending(cls, receiving_id: int) -> Tuple[Dict[str, Any], int]:
        rcv = PurchaseReceivingRepository.get_by_id(receiving_id)
        if not rcv:
            return ServiceResponse.not_found("Receiving not found")

        if rcv.status != PurchaseReceiving.Status.DRAFT:
            return ServiceResponse.error("Cannot add items to completed receiving")

        # Need purchase_order for items access
        rcv = PurchaseReceiving.objects.select_related("purchase_order").get(id=rcv.id)

        added = 0
        for po_item in rcv.purchase_order.items.all():
            pending = po_item.quantity_ordered - po_item.quantity_received
            if pending > 0:
                result, status = cls.add(
                    receiving_id=receiving_id,
                    po_item_id=po_item.id,
                    quantity_received=pending,
                    unit_cost=po_item.unit_price,
                )
                if status >= 400:
                    return result, status
                added += 1

        return ServiceResponse.success(data={
            "items_added": added
        }, message=f"{added} items added to receiving")
