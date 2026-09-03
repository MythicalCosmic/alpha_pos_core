from typing import Dict, Any, List, Tuple
from decimal import Decimal
from datetime import date, timedelta
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from base.helpers.response import ServiceResponse
from base.money import MoneyValueError, decimal_value, local_iso
from stock.models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseReceiving, PurchaseReceivingItem,
    PurchaseReceivingCorrection,
    Supplier, SupplierStockItem, StockBatch, StockItem, StockItemUnit, StockSettings,
    StockTransaction,
)
from stock.services.base_service import generate_number, round_decimal, to_decimal
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


def _base_receipt_values(item, item_unit_factors):
    quantity = to_decimal(item.quantity_received)
    unit_cost = to_decimal(item.unit_cost)
    if item.unit_id == item.stock_item.base_unit_id:
        factor = Decimal('1')
    else:
        factor = item_unit_factors.get((item.stock_item_id, item.unit_id))
        if factor is None and item.unit.base_unit_id == item.stock_item.base_unit_id:
            factor = to_decimal(item.unit.conversion_factor)
    if factor is None or factor <= 0:
        return None
    base_quantity = round_decimal(quantity * factor, 4)
    if base_quantity <= 0:
        return None
    base_cost = round_decimal(quantity * unit_cost / base_quantity, 4)
    if (
        factor > Decimal('999999999.999999')
        or base_quantity > Decimal('99999999999.9999')
        or base_cost > Decimal('99999999999.9999')
    ):
        return None
    return {
        'factor': factor,
        'quantity': base_quantity,
        'unit_id': item.stock_item.base_unit_id,
        'unit_cost': base_cost,
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

            "subtotal_uzs": int(po.subtotal),
            "subtotal": int(po.subtotal),
            "tax_amount_uzs": int(po.tax_amount),
            "tax_amount": int(po.tax_amount),
            "shipping_cost_uzs": int(po.shipping_cost),
            "shipping_cost": int(po.shipping_cost),
            "discount_uzs": int(po.discount),
            "discount": int(po.discount),
            "total_uzs": int(po.total),
            "total": int(po.total),
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
            "total_uzs": int(po.total),
            "total": int(po.total),
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
             location_id: int = None,
             branch_id: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.select_related("supplier", "delivery_location")
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id, is_deleted=False)

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
    def get(cls, po_id: int, include_receivings: bool = True,
            branch_id: str = None) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.filter(pk=po_id, is_deleted=False)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        po = queryset.select_related(
            'supplier', 'delivery_location', 'created_by', 'approved_by',
        ).prefetch_related(
            'items__stock_item', 'items__unit', 'receivings',
        ).first()

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
               items: List[Dict] = None,
               branch_id: str = None) -> Tuple[Dict[str, Any], int]:

        supplier_filters = {'id': supplier_id, 'is_active': True}
        location_filters = {'id': delivery_location_id, 'is_active': True}
        if branch_id:
            supplier_filters['branch_id'] = branch_id
            location_filters['branch_id'] = branch_id
        supplier = SupplierRepository.first(**supplier_filters)
        if not supplier:
            return ServiceResponse.not_found("Supplier not found")

        location = StockLocationRepository.first(**location_filters)
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
            branch_id=branch_id or supplier.branch_id,
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
                    branch_id=branch_id,
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
    def update(cls, po_id: int, branch_id=None, **kwargs) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.filter(pk=po_id, is_deleted=False)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        po = queryset.first()
        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        if po.status != PurchaseOrder.Status.DRAFT:
            return ServiceResponse.error("Can only update orders in DRAFT status")

        update_fields = ["updated_at"]

        if "supplier_id" in kwargs:
            filters = {'id': kwargs['supplier_id'], 'is_active': True}
            if branch_id:
                filters['branch_id'] = branch_id
            supplier = SupplierRepository.first(**filters)
            if not supplier:
                return ServiceResponse.not_found("Supplier not found")
            po.supplier = supplier
            update_fields.append("supplier")

        if "delivery_location_id" in kwargs:
            filters = {'id': kwargs['delivery_location_id'], 'is_active': True}
            if branch_id:
                filters['branch_id'] = branch_id
            location = StockLocationRepository.first(**filters)
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
    def send(cls, po_id: int, branch_id=None) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.filter(pk=po_id, is_deleted=False)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        po = queryset.first()
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
    def confirm(cls, po_id: int, approved_by_id: int = None,
                branch_id=None) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.filter(pk=po_id, is_deleted=False)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        po = queryset.first()
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
    def cancel(cls, po_id: int, reason: str = "",
               branch_id=None) -> Tuple[Dict[str, Any], int]:
        queryset = PurchaseOrder.objects.filter(pk=po_id, is_deleted=False)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        po = queryset.first()
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
        return ServiceResponse.failure(
            'UNFUNDED_PAYMENT_ROUTE_RETIRED',
            'Use the funded supplier-payment endpoint with SAFE or BANK.',
            410,
            details={'purchase_order_id': po_id},
        )

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
            "quantity_canceled": str(item.quantity_canceled),
            "quantity_pending": str(max(
                Decimal('0'), item.quantity_ordered - item.quantity_received - item.quantity_canceled,
            )),
            "unit": item.unit.short_name,
            "unit_price_uzs": int(item.unit_price),
            "unit_price": int(item.unit_price),
            "discount_percent": str(item.discount_percent),
            "tax_percent": str(item.tax_percent),
            "total_price_uzs": int(item.total_price),
            "total_price": int(item.total_price),
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
            notes: str = "",
            branch_id: str = None) -> Tuple[Dict[str, Any], int]:

        po_filters = {'id': purchase_order_id}
        if branch_id:
            po_filters['branch_id'] = branch_id
        po = PurchaseOrderRepository.first(**po_filters)
        if not po:
            return ServiceResponse.not_found("Purchase order not found")

        if po.status != PurchaseOrder.Status.DRAFT:
            return ServiceResponse.error("Can only add items to DRAFT orders")

        item_filters = {'id': stock_item_id}
        if branch_id:
            item_filters['branch_id'] = branch_id
        stock_item = StockItemRepository.first(**item_filters)
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
            branch_id=po.branch_id,
        )

        PurchaseOrderService._recalculate_totals(purchase_order_id)

        return ServiceResponse.success(data={
            "id": item.id,
            "item": cls.serialize(item)
        }, message="Item added to order")

    @classmethod
    @transaction.atomic
    def update_item(cls, item_id: int, branch_id=None, **kwargs) -> Tuple[Dict[str, Any], int]:
        filters = {'id': item_id}
        if branch_id:
            filters['purchase_order__branch_id'] = branch_id
        item = PurchaseOrderItemRepository.first(**filters)
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
    def remove(cls, item_id: int, branch_id=None) -> Tuple[Dict[str, Any], int]:
        filters = {'id': item_id}
        if branch_id:
            filters['purchase_order__branch_id'] = branch_id
        item = PurchaseOrderItemRepository.first(**filters)
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
            "completed_at": local_iso(rcv.completed_at),
            "supplier_balance_before_uzs": (
                int(rcv.supplier_balance_before)
                if rcv.supplier_balance_before is not None else None
            ),
            "supplier_balance_after_uzs": (
                int(rcv.supplier_balance_after)
                if rcv.supplier_balance_after is not None else None
            ),
            "received_value_uzs": (
                int(rcv.received_value_uzs)
                if rcv.received_value_uzs is not None else None
            ),
            "supplier_transaction_id": rcv.supplier_transaction_id,
            "quality_posting_policy": rcv.quality_posting_policy or None,
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
               notes: str = "",
               actor=None,
               branch_id: str = None) -> Tuple[Dict[str, Any], int]:

        from base.services.branch_scope import resolve_actor_branch

        if actor is None:
            from base.models import User

            actor = User.objects.filter(
                pk=received_by_id,
                is_deleted=False,
            ).first()
        actor_branch = str(resolve_actor_branch(actor) or '').strip()
        requested_branch = str(branch_id or '').strip()
        if actor is not None and requested_branch and requested_branch != actor_branch:
            return ServiceResponse.failure(
                'LOCATION_FORBIDDEN',
                'Receiving branch is outside the authorized scope.',
                403,
            )
        branch_id = actor_branch if actor is not None else requested_branch
        if actor is not None:
            received_by_id = actor.id
        po_filters = {'id': purchase_order_id}
        if branch_id:
            po_filters['branch_id'] = branch_id
        po = PurchaseOrderRepository.first(**po_filters)
        if not po:
            return ServiceResponse.not_found("Purchase order not found")
        branch_id = branch_id or po.branch_id
        if not branch_id:
            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED',
                'Receiving branch could not be resolved.',
                403,
            )

        if po.status not in [PurchaseOrder.Status.CONFIRMED, PurchaseOrder.Status.PARTIAL]:
            return ServiceResponse.error(f"Cannot receive order in {po.status} status")

        location_id = location_id or po.delivery_location_id

        location = StockLocationRepository.first(
            id=location_id,
            branch_id=branch_id,
            is_active=True,
        )
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
            branch_id=branch_id,
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
    def complete(cls, receiving_id: int, *, actor=None, action_id=None,
                 idempotency_key='') -> Tuple[Dict[str, Any], int]:
        identity = PurchaseReceiving.objects.filter(
            pk=receiving_id, is_deleted=False,
        ).values(
            'purchase_order_id', 'purchase_order__supplier_id',
        ).first()
        if not identity:
            return ServiceResponse.not_found("Receiving not found")
        supplier = Supplier.objects.select_for_update().get(
            pk=identity['purchase_order__supplier_id'],
        )
        po = PurchaseOrder.objects.select_for_update().get(
            pk=identity['purchase_order_id'],
        )
        rcv = PurchaseReceiving.objects.select_for_update().select_related(
            'purchase_order', 'location', 'received_by',
        ).get(pk=receiving_id, is_deleted=False)
        if rcv.purchase_order_id != po.id or po.supplier_id != supplier.id:
            return ServiceResponse.conflict(
                'RECEIVING_RELATIONSHIP_CHANGED',
                'Receiving ownership changed while it was being locked; retry safely.',
            )
        if actor is not None:
            from base.services.branch_scope import resolve_actor_branch

            actor_branch = str(resolve_actor_branch(actor) or '').strip()
            global_admin = (
                actor.role == 'ADMIN'
                and str(actor.branch_id or '').strip().lower() in {'', 'cloud'}
            )
            if not global_admin and (
                not actor_branch or actor_branch != rcv.branch_id
            ):
                return ServiceResponse.failure(
                    'LOCATION_FORBIDDEN',
                    'Receiving is outside the authorized branch.',
                    403,
                )

        if rcv.status == PurchaseReceiving.Status.COMPLETED:
            return cls._completion_response(rcv)
        if rcv.status != PurchaseReceiving.Status.DRAFT:
            return ServiceResponse.error("Receiving cannot be completed")

        if not rcv.items.exists():
            return ServiceResponse.error("No items in receiving")

        settings = StockSettings.load()
        if not settings.stock_enabled:
            return ServiceResponse.conflict(
                'STOCK_SYSTEM_DISABLED',
                'Receiving cannot complete while stock tracking is disabled.',
            )
        if supplier.currency != 'UZS' or po.currency != 'UZS':
            return ServiceResponse.failure(
                'SUPPLIER_CURRENCY_UNSUPPORTED',
                'Receiving can post supplier debt only in UZS.',
                422,
                errors={'currency': ['Only UZS is supported.']},
            )
        receiving_items = list(
            rcv.items.select_related(
                'stock_item__base_unit', 'unit__base_unit', 'po_item',
            ).order_by('stock_item_id', 'id')
        )
        if any(item.quality_status == PurchaseReceivingItem.QualityStatus.PENDING
               for item in receiving_items):
            return ServiceResponse.validation_error(
                errors={'quality_status': 'Resolve all PENDING quality results before completion'},
            )
        item_unit_factors = {
            (link.stock_item_id, link.unit_id): link.conversion_to_base
            for link in StockItemUnit.objects.filter(
                stock_item_id__in={item.stock_item_id for item in receiving_items},
                unit_id__in={item.unit_id for item in receiving_items},
                is_deleted=False,
            ).order_by('stock_item_id', 'unit_id', 'id')
        }
        errors = {}
        base_values = {}
        for item in receiving_items:
            if item.unit_cost < 0:
                errors[f'items.{item.id}.unit_cost'] = 'Must be non-negative'
            if item.stock_item.track_batches and not item.batch_number.strip():
                errors[f'items.{item.id}.batch_number'] = 'Required for batch-tracked items'
            if item.stock_item.track_expiry and not item.expiry_date:
                errors[f'items.{item.id}.expiry_date'] = 'Required for expiry-tracked items'
            if item.quality_status == PurchaseReceivingItem.QualityStatus.FAILED and not item.notes.strip():
                errors[f'items.{item.id}.notes'] = 'Required for failed quality'
            if item.quality_status == PurchaseReceivingItem.QualityStatus.PASSED:
                values = _base_receipt_values(item, item_unit_factors)
                if values is None:
                    errors[f'items.{item.id}.unit_id'] = (
                        'A valid item-specific conversion to the base unit is required'
                    )
                else:
                    base_values[item.id] = values
        received_value = sum(
            (to_decimal(item.unit_cost) * to_decimal(item.quantity_received)
             for item in receiving_items
             if item.quality_status == PurchaseReceivingItem.QualityStatus.PASSED),
            Decimal('0'),
        )
        if received_value != received_value.to_integral_value():
            errors['received_total_uzs'] = 'Receiving total must be a whole UZS amount'
        if errors:
            return ServiceResponse.validation_error(errors=errors)

        # Lock every affected PO line and validate the aggregate under one PO
        # lock. This closes both the multiple-lines-in-one-draft hole and the
        # concurrent-receivings over-post race.
        line_ids = sorted({item.po_item_id for item in receiving_items})
        locked_lines = {
            line.id: line for line in PurchaseOrderItem.objects.select_for_update()
            .filter(id__in=line_ids).order_by('id')
        }
        for line_id in line_ids:
            passed_qty = sum(
                (item.quantity_received for item in receiving_items
                 if item.po_item_id == line_id
                 and item.quality_status == PurchaseReceivingItem.QualityStatus.PASSED),
                Decimal('0'),
            )
            line = locked_lines[line_id]
            allowed = line.quantity_ordered - line.quantity_canceled
            if line.quantity_received + passed_qty > allowed and not rcv.over_receipt_approved_by_id:
                return ({
                    'success': False,
                    'message': 'Manager approval is required for over-receipt',
                    'errors': {'po_item_id': line_id, 'code': 'over_receipt_approval_required'},
                }, 409)

        for item in receiving_items:
            # A failed inspection remains immutable evidence but never creates
            # usable stock or supplier debt.
            if item.quality_status == PurchaseReceivingItem.QualityStatus.FAILED:
                continue
            base_value = base_values[item.id]
            item.conversion_to_base_snapshot = base_value['factor']
            item.base_quantity = base_value['quantity']
            item.base_unit_id = base_value['unit_id']
            item.base_unit_cost = base_value['unit_cost']
            item.save(update_fields=[
                'conversion_to_base_snapshot', 'base_quantity', 'base_unit',
                'base_unit_cost',
            ])
            batch = None
            if settings.track_batches or item.stock_item.track_batches:
                from .batch_service import StockBatchService
                batch_result, batch_status = StockBatchService.create(
                    stock_item_id=item.stock_item_id,
                    location_id=rcv.location_id,
                    quantity=base_value['quantity'],
                    unit_cost=base_value['unit_cost'],
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
                quantity=base_value['quantity'],
                movement_type="PURCHASE_IN",
                user_id=rcv.received_by_id,
                unit_id=base_value['unit_id'],
                batch_id=batch.id if batch else None,
                reference_type="PurchaseReceiving",
                reference_id=rcv.id,
                unit_cost=base_value['unit_cost'],
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
            po_item = locked_lines[item.po_item_id]
            po_item.quantity_received += item.quantity_received
            po_item.save(update_fields=['quantity_received'])

            from .item_service import StockItemService
            cost_result, cost_status = StockItemService.update_cost(
                item.stock_item_id, base_value['unit_cost'], "AVG",
                received_qty=base_value['quantity'],
            )
            if cost_status >= 400:
                transaction.set_rollback(True)
                return cost_result, cost_status

        cls._update_po_status(po)

        # Record the supplier debt: receiving goods worth `received_value` means
        # we now owe the supplier that much (a PURCHASE ledger row). Previously
        # the debt was never recorded — the money owed vanished.
        before = supplier.current_balance
        after = before
        supplier_txn = None
        if received_value > 0 and po.supplier_id:
            from .supplier_ledger_service import SupplierLedgerService
            supplier_txn = SupplierLedgerService.record_purchase(
                po.supplier_id, received_value,
                reference_type="PurchaseReceiving", reference_id=rcv.id,
                performed_by=rcv.received_by,
            )
            if supplier_txn is None:
                transaction.set_rollback(True)
                return ServiceResponse.not_found('Supplier not found')
            before = supplier_txn.balance_before
            after = supplier_txn.balance_after

        rcv.status = PurchaseReceiving.Status.COMPLETED
        rcv.completed_at = timezone.now()
        rcv.supplier_balance_before = before
        rcv.supplier_balance_after = after
        rcv.received_value_uzs = received_value
        rcv.supplier_transaction = supplier_txn if received_value > 0 else None
        rcv.completion_action_id = action_id
        rcv.completion_idempotency_key = idempotency_key or ''
        rcv.quality_posting_policy = 'PASSED_ONLY_PENDING_BLOCKS_COMPLETION'
        rcv.save(update_fields=[
            "status", "completed_at", "supplier_balance_before",
            "supplier_balance_after", "received_value_uzs",
            "supplier_transaction", "completion_action_id",
            "completion_idempotency_key", "quality_posting_policy", "updated_at",
        ])

        return cls._completion_response(rcv)

    @classmethod
    def _completion_response(cls, rcv):
        stock_transaction_ids = list(StockTransaction.objects.filter(
            branch_id=rcv.branch_id,
            is_deleted=False,
            reference_type='PurchaseReceiving',
            reference_id=rcv.id,
        ).order_by('id').values_list('id', flat=True))
        return ServiceResponse.success(data={
            'receiving_id': rcv.id,
            'receiving_uuid': str(rcv.uuid),
            'status': 'COMPLETE',
            'supplier_id': rcv.purchase_order.supplier_id,
            'supplier_uuid': str(rcv.purchase_order.supplier.uuid),
            'supplier_balance_before_uzs': (
                int(rcv.supplier_balance_before)
                if rcv.supplier_balance_before is not None else None
            ),
            'supplier_balance_after_uzs': (
                int(rcv.supplier_balance_after)
                if rcv.supplier_balance_after is not None else None
            ),
            'received_value_uzs': (
                int(rcv.received_value_uzs)
                if rcv.received_value_uzs is not None else None
            ),
            'stock_transaction_ids': stock_transaction_ids,
            'supplier_transaction_id': rcv.supplier_transaction_id,
            'quality_posting_policy': (
                rcv.quality_posting_policy
                or 'PASSED_ONLY_PENDING_BLOCKS_COMPLETION'
            ),
            'posted_at': local_iso(rcv.completed_at),
            'timezone': 'Asia/Tashkent',
            'receiving': cls.serialize(rcv),
        }, message='Receiving completed')

    @classmethod
    @transaction.atomic
    def request_correction(cls, receiving_id, requested_by, reason):
        rcv = PurchaseReceiving.objects.select_for_update().filter(
            id=receiving_id, is_deleted=False,
        ).first()
        if not rcv:
            return ServiceResponse.not_found('Receiving not found')
        if rcv.status != PurchaseReceiving.Status.COMPLETED:
            return ({'success': False, 'message': 'Only completed receiving can be corrected',
                     'errors': {'code': 'receiving_not_completed'}}, 409)
        reason = str(reason or '').strip()
        if not reason:
            return ServiceResponse.validation_error(errors={'reason': 'Required'})
        existing = rcv.corrections.filter(status=PurchaseReceivingCorrection.Status.PENDING,
                                          is_deleted=False).first()
        if existing:
            return ServiceResponse.success(data={'correction_id': existing.id,
                                                 'status': existing.status})
        row = PurchaseReceivingCorrection.objects.create(
            receiving=rcv, reason=reason, requested_by=requested_by,
            branch_id=rcv.branch_id,
        )
        return ServiceResponse.created(data={'correction_id': row.id, 'status': row.status})

    @classmethod
    @transaction.atomic
    def review_correction(cls, correction_id, reviewer, approve, note):
        identity = PurchaseReceivingCorrection.objects.filter(
            id=correction_id, is_deleted=False,
        ).values(
            'receiving_id', 'receiving__purchase_order_id',
            'receiving__purchase_order__supplier_id',
        ).first()
        if not identity:
            return ServiceResponse.not_found('Receiving correction not found')
        po = None
        rcv = None
        if approve:
            _supplier = Supplier.objects.select_for_update().get(
                pk=identity['receiving__purchase_order__supplier_id'],
            )
            po = PurchaseOrder.objects.select_for_update().get(
                pk=identity['receiving__purchase_order_id'],
            )
            rcv = PurchaseReceiving.objects.select_for_update().get(
                pk=identity['receiving_id'],
            )
        correction = PurchaseReceivingCorrection.objects.select_for_update(
            of=('self',),
        ).select_related(
            'receiving__purchase_order__supplier', 'requested_by', 'reviewed_by',
        ).filter(id=correction_id, is_deleted=False).first()
        if not correction:
            return ServiceResponse.not_found('Receiving correction not found')
        if correction.status == PurchaseReceivingCorrection.Status.APPROVED:
            return ServiceResponse.success(data={
                'correction_id': correction.id, 'status': correction.status,
                'supplier_balance_before_uzs': int(correction.supplier_balance_before or 0),
                'supplier_balance_after_uzs': int(correction.supplier_balance_after or 0),
            })
        if correction.status != PurchaseReceivingCorrection.Status.PENDING:
            return ({'success': False, 'message': 'Correction already reviewed',
                     'errors': {'code': 'correction_already_reviewed'}}, 409)
        if correction.requested_by_id == reviewer.id:
            return ({'success': False, 'message': 'You cannot approve your own correction',
                     'errors': {'code': 'self_approval_forbidden'}}, 403)
        note = str(note or '').strip()
        if not note:
            return ServiceResponse.validation_error(errors={'review_note': 'Required'})
        if not approve:
            correction.status = PurchaseReceivingCorrection.Status.REJECTED
            correction.reviewed_by = reviewer
            correction.reviewed_at = timezone.now()
            correction.review_note = note
            correction.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note'])
            return ServiceResponse.success(data={'correction_id': correction.id,
                                                 'status': correction.status})

        if (
            correction.receiving_id != rcv.id
            or rcv.purchase_order_id != po.id
            or po.supplier_id != _supplier.id
        ):
            return ServiceResponse.conflict(
                'RECEIVING_RELATIONSHIP_CHANGED',
                'Receiving ownership changed while it was being locked; retry safely.',
            )

        if rcv.corrections.filter(status=PurchaseReceivingCorrection.Status.APPROVED,
                                  is_deleted=False).exclude(pk=correction.pk).exists():
            return ({'success': False, 'message': 'Receiving was already reversed',
                     'errors': {'code': 'receiving_already_reversed'}}, 409)
        items = list(rcv.items.select_related(
            'stock_item__base_unit', 'unit__base_unit', 'base_unit',
            'po_item', 'batch_created',
        ).order_by('stock_item_id', 'id'))
        passed = [item for item in items
                  if item.quality_status == PurchaseReceivingItem.QualityStatus.PASSED]
        item_unit_factors = {
            (link.stock_item_id, link.unit_id): link.conversion_to_base
            for link in StockItemUnit.objects.filter(
                stock_item_id__in={item.stock_item_id for item in passed},
                unit_id__in={item.unit_id for item in passed},
                is_deleted=False,
            ).order_by('stock_item_id', 'unit_id', 'id')
        }
        base_values = {}
        for item in passed:
            if (
                item.base_quantity is not None
                and item.base_unit_id is not None
                and item.base_unit_cost is not None
            ):
                base_values[item.id] = {
                    'quantity': item.base_quantity,
                    'unit_id': item.base_unit_id,
                    'unit_cost': item.base_unit_cost,
                }
            else:
                base_value = _base_receipt_values(item, item_unit_factors)
                if base_value is None:
                    return ServiceResponse.conflict(
                        'RECEIVING_UNIT_CONVERSION_MISSING',
                        'The original receiving unit cannot be converted safely.',
                        details={'receiving_item_id': item.id},
                    )
                base_values[item.id] = base_value
        from stock.models import StockBatch
        from .level_service import StockLevelService
        list(StockItem.objects.select_for_update().filter(
            pk__in=sorted({item.stock_item_id for item in passed}),
        ).order_by('pk'))
        batches = {}
        locked_batches = {
            batch.id: batch
            for batch in StockBatch.objects.select_for_update().filter(
                pk__in=sorted({
                    item.batch_created_id for item in passed
                    if item.batch_created_id
                }),
            ).order_by('pk')
        }
        for item in passed:
            base_value = base_values[item.id]
            level = StockLevelService.get_level_for_update(item.stock_item_id, rcv.location_id)
            if level.quantity < base_value['quantity']:
                return ({'success': False, 'message': 'Received stock has already been consumed',
                         'errors': {'code': 'receiving_reversal_stock_unavailable',
                                    'stock_item_id': item.stock_item_id}}, 409)
            if item.batch_created_id:
                batch = locked_batches[item.batch_created_id]
                if batch.current_quantity < base_value['quantity']:
                    return ({'success': False, 'message': 'Received batch has already been consumed',
                             'errors': {'code': 'receiving_reversal_batch_unavailable',
                                        'batch_id': batch.id}}, 409)
                batches[item.id] = batch
        line_ids = sorted({item.po_item_id for item in passed})
        lines = {line.id: line for line in PurchaseOrderItem.objects.select_for_update()
                 .filter(id__in=line_ids).order_by('id')}
        reversed_value = Decimal('0')
        for item in passed:
            base_value = base_values[item.id]
            result, status = StockLevelService.adjust(
                stock_item_id=item.stock_item_id, location_id=rcv.location_id,
                quantity=base_value['quantity'], movement_type='RETURN_TO_SUPPLIER',
                user_id=reviewer.id, unit_id=base_value['unit_id'],
                batch_id=item.batch_created_id,
                reference_type='PurchaseReceivingCorrection', reference_id=correction.id,
                unit_cost=base_value['unit_cost'], notes=note,
            )
            if status >= 400:
                transaction.set_rollback(True)
                return result, status
            from .item_service import StockItemService

            cost_result, cost_status = StockItemService.reverse_received_cost(
                item.stock_item_id,
                base_value['quantity'],
                base_value['unit_cost'],
            )
            if cost_status >= 400:
                transaction.set_rollback(True)
                return cost_result, cost_status
            if item.id in batches:
                batch = batches[item.id]
                batch.current_quantity -= base_value['quantity']
                batch.save(update_fields=['current_quantity', 'updated_at'])
            line = lines[item.po_item_id]
            line.quantity_received = max(Decimal('0'), line.quantity_received - item.quantity_received)
            line.save(update_fields=['quantity_received'])
            reversed_value += item.unit_cost * item.quantity_received
        cls._update_po_status(po)
        from .supplier_ledger_service import SupplierLedgerService
        supplier_txn = SupplierLedgerService.record_return(
            po.supplier_id, reversed_value, reference_type='PurchaseReceivingCorrection',
            reference_id=correction.id, performed_by=reviewer, note=note,
        )
        correction.status = PurchaseReceivingCorrection.Status.APPROVED
        correction.reviewed_by = reviewer
        correction.reviewed_at = timezone.now()
        correction.review_note = note
        correction.supplier_balance_before = supplier_txn.balance_before
        correction.supplier_balance_after = supplier_txn.balance_after
        correction.save()
        return ServiceResponse.success(data={
            'correction_id': correction.id, 'status': correction.status,
            'supplier_balance_before_uzs': int(correction.supplier_balance_before),
            'supplier_balance_after_uzs': int(correction.supplier_balance_after),
            'reversed_at': correction.reviewed_at.isoformat(),
        })

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
            try:
                quantity_received = decimal_value(
                    quantity_received,
                    'quantity_received',
                    places=4,
                    positive=True,
                    maximum='99999999999.9999',
                )
            except MoneyValueError as exc:
                return ServiceResponse.validation_error(
                    errors={'quantity_received': str(exc)},
                )

            item.quantity_received = quantity_received

        if batch_number is not None:
            item.batch_number = batch_number

        if expiry_date is not None:
            item.expiry_date = expiry_date

        if unit_cost is not None:
            try:
                unit_cost = decimal_value(
                    unit_cost,
                    'unit_cost',
                    places=4,
                    maximum='99999999999.9999',
                )
            except MoneyValueError as exc:
                return ServiceResponse.validation_error(
                    errors={'unit_cost': str(exc)},
                )
            item.unit_cost = unit_cost

        if quality_status is not None:
            if quality_status not in PurchaseReceivingItem.QualityStatus.values:
                return ServiceResponse.validation_error(
                    errors={'quality_status': 'Must be PASSED, FAILED, or PENDING'},
                )
            item.quality_status = quality_status

        if notes is not None:
            item.notes = notes

        if item.stock_item.track_batches and not item.batch_number.strip():
            return ServiceResponse.validation_error(
                errors={'batch_number': 'Required for batch-tracked items'},
            )
        if item.stock_item.track_expiry and not item.expiry_date:
            return ServiceResponse.validation_error(
                errors={'expiry_date': 'Required for expiry-tracked items'},
            )
        pending = max(
            Decimal('0'), item.po_item.quantity_ordered
            - item.po_item.quantity_received - item.po_item.quantity_canceled,
        )
        tolerance = StockSettings.load().receiving_quantity_tolerance_percent or Decimal('0')
        variance_percent = (
            abs(item.quantity_received - pending) * Decimal('100') / pending
            if pending else Decimal('100')
        )
        if (item.quality_status == PurchaseReceivingItem.QualityStatus.FAILED
                or variance_percent > tolerance) and not item.notes.strip():
            return ServiceResponse.validation_error(
                errors={'notes': 'Required for failed quality or quantity variance'},
            )

        item.save()

        return ServiceResponse.success(data={
            "item": PurchaseReceivingItemService.serialize(item)
        }, message="Receiving item updated")

    @classmethod
    def _update_po_status(cls, po: PurchaseOrder):
        items = po.items.all()

        fully_received = all(
            item.quantity_received >= item.quantity_ordered - item.quantity_canceled
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
            po.received_date = None
        else:
            po.status = PurchaseOrder.Status.CONFIRMED
            po.received_date = None

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
            "conversion_to_base_snapshot": (
                str(item.conversion_to_base_snapshot)
                if item.conversion_to_base_snapshot is not None else None
            ),
            "base_quantity": (
                str(item.base_quantity) if item.base_quantity is not None else None
            ),
            "base_unit_id": item.base_unit_id,
            "base_unit_cost_uzs": (
                str(item.base_unit_cost)
                if item.base_unit_cost is not None else None
            ),
            "batch_number": item.batch_number,
            "expiry_date": item.expiry_date.isoformat() if item.expiry_date else None,
            "unit_cost_uzs": int(item.unit_cost),
            "unit_cost": int(item.unit_cost),
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

        try:
            quantity_received = decimal_value(
                quantity_received,
                'quantity_received',
                places=4,
                positive=True,
                maximum='99999999999.9999',
            )
        except MoneyValueError as exc:
            return ServiceResponse.validation_error(
                errors={'quantity_received': str(exc)},
            )

        already_received = po_item.quantity_received
        pending = max(
            Decimal('0'), po_item.quantity_ordered - already_received - po_item.quantity_canceled,
        )

        try:
            unit_cost = decimal_value(
                unit_cost if unit_cost is not None else po_item.unit_price,
                'unit_cost',
                places=4,
                maximum='99999999999.9999',
            )
        except MoneyValueError as exc:
            return ServiceResponse.validation_error(
                errors={'unit_cost': str(exc)},
            )
        if quality_status not in PurchaseReceivingItem.QualityStatus.values:
            return ServiceResponse.validation_error(
                errors={'quality_status': 'Must be PASSED, FAILED, or PENDING'},
            )
        if po_item.stock_item.track_batches and not str(batch_number or '').strip():
            return ServiceResponse.validation_error(
                errors={'batch_number': 'Required for batch-tracked items'},
            )
        if po_item.stock_item.track_expiry and not expiry_date:
            return ServiceResponse.validation_error(
                errors={'expiry_date': 'Required for expiry-tracked items'},
            )

        tolerance = StockSettings.load().receiving_quantity_tolerance_percent or Decimal('0')
        variance_percent = (
            abs(quantity_received - pending) * Decimal('100') / pending
            if pending else Decimal('100')
        )
        if (quality_status == PurchaseReceivingItem.QualityStatus.FAILED
                or variance_percent > tolerance) and not str(notes or '').strip():
            return ServiceResponse.validation_error(
                errors={'notes': 'Required for failed quality or quantity variance'},
            )

        item = PurchaseReceivingItemRepository.create(
            receiving=rcv,
            po_item=po_item,
            stock_item=po_item.stock_item,
            quantity_received=quantity_received,
            unit=po_item.unit,
            batch_number=batch_number,
            expiry_date=expiry_date,
            unit_cost=unit_cost,
            quality_status=quality_status,
            notes=notes,
            branch_id=rcv.branch_id,
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
