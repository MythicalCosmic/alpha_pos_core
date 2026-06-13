from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum, Count, Avg
from django.utils import timezone

from base.helpers.response import ServiceResponse
from stock.models import Supplier, SupplierStockItem, PurchaseOrder
from stock.services.base_service import to_decimal
from stock.repositories import (
    SupplierRepository, SupplierStockItemRepository,
    StockItemRepository, StockUnitRepository,
)


class SupplierService:

    @classmethod
    def serialize(cls, supplier: Supplier,
                  include_items: bool = False,
                  include_stats: bool = False) -> Dict[str, Any]:
        data = {
            "id": supplier.id,
            "uuid": str(supplier.uuid),
            "code": supplier.code,
            "name": supplier.name,
            "legal_name": supplier.legal_name,

            "contact_person": supplier.contact_person,
            "email": supplier.email,
            "phone": supplier.phone,
            "mobile": supplier.mobile,

            "address": supplier.address,
            "city": supplier.city,
            "country": supplier.country,
            "tax_id": supplier.tax_id,

            "payment_terms_days": supplier.payment_terms_days,
            "credit_limit": str(supplier.credit_limit) if supplier.credit_limit else None,
            "current_balance": str(supplier.current_balance),
            "currency": supplier.currency,

            "lead_time_days": supplier.lead_time_days,
            "minimum_order_value": str(supplier.minimum_order_value) if supplier.minimum_order_value else None,

            "rating": supplier.rating,
            "is_active": supplier.is_active,
            "notes": supplier.notes,
            "created_at": supplier.created_at.isoformat(),
        }

        if include_items:
            items_qs = SupplierStockItemRepository.get_for_supplier(supplier.id)
            data["items"] = [
                SupplierStockItemService.serialize(si)
                for si in items_qs
            ]
            data["item_count"] = items_qs.count()

        if include_stats:
            po_stats = PurchaseOrder.objects.filter(supplier=supplier).aggregate(
                total_orders=Count("id"),
                total_value=Sum("total"),
                avg_order_value=Avg("total")
            )
            data["stats"] = {
                "total_orders": po_stats["total_orders"] or 0,
                "total_value": str(po_stats["total_value"] or 0),
                "avg_order_value": str(po_stats["avg_order_value"] or 0),
            }

        return data

    @classmethod
    def serialize_brief(cls, supplier: Supplier) -> Dict[str, Any]:
        return {
            "id": supplier.id,
            "uuid": str(supplier.uuid),
            "code": supplier.code,
            "name": supplier.name,
            "city": supplier.city,
            "rating": supplier.rating,
            "is_active": supplier.is_active,
        }

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             search: str = None,
             active_only: bool = True,
             has_items_only: bool = False) -> Tuple[Dict[str, Any], int]:
        if active_only:
            queryset = SupplierRepository.get_active()
        else:
            queryset = SupplierRepository.get_all()

        if search:
            queryset = SupplierRepository.search(queryset, search)

        if has_items_only:
            queryset = SupplierRepository.with_item_count(queryset).filter(item_count__gt=0)

        queryset = queryset.order_by("name")

        page_obj, paginator = SupplierRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "suppliers": [cls.serialize_brief(s) for s in page_obj.object_list],
            "pagination": {
                "current_page": page_obj.number,
                "total_pages": paginator.num_pages,
                "total_suppliers": paginator.count,
                "per_page": per_page,
                "has_next": page_obj.has_next(),
                "has_previous": page_obj.has_previous(),
            }
        })

    @classmethod
    def search(cls, query: str, limit: int = 20) -> Tuple[Dict[str, Any], int]:
        queryset = SupplierRepository.get_active()
        suppliers = SupplierRepository.search(queryset, query).order_by("name")[:limit]

        return ServiceResponse.success(data={
            "suppliers": [cls.serialize_brief(s) for s in suppliers],
            "count": len(suppliers)
        })

    @classmethod
    def get_for_item(cls, stock_item_id: int) -> Tuple[Dict[str, Any], int]:
        supplier_items = SupplierStockItemRepository.get_for_item(
            stock_item_id
        ).filter(
            supplier__is_active=True
        ).order_by("-is_preferred", "price")

        suppliers = []
        for si in supplier_items:
            suppliers.append({
                "supplier": cls.serialize_brief(si.supplier),
                "supplier_sku": si.supplier_sku,
                "supplier_name": si.supplier_name,
                "price": str(si.price),
                "currency": si.currency,
                "min_order_qty": str(si.min_order_qty),
                "pack_size": str(si.pack_size),
                "lead_time_days": si.lead_time_days,
                "is_preferred": si.is_preferred,
            })

        return ServiceResponse.success(data={
            "suppliers": suppliers,
            "count": len(suppliers)
        })

    @classmethod
    def get(cls, supplier_id: int,
            include_items: bool = True,
            include_stats: bool = True) -> Tuple[Dict[str, Any], int]:
        supplier = SupplierRepository.get_by_id(supplier_id)
        if not supplier:
            return ServiceResponse.not_found(f"Supplier with id {supplier_id} not found")

        return ServiceResponse.success(data={
            "supplier": cls.serialize(supplier, include_items=include_items, include_stats=include_stats)
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               name: str,
               code: str = None,
               legal_name: str = "",
               contact_person: str = "",
               email: str = "",
               phone: str = "",
               mobile: str = "",
               address: str = "",
               city: str = "",
               country: str = "",
               tax_id: str = "",
               payment_terms_days: int = 30,
               credit_limit: Decimal = None,
               currency: str = "UZS",
               lead_time_days: int = 1,
               minimum_order_value: Decimal = None,
               rating: int = None,
               notes: str = "") -> Tuple[Dict[str, Any], int]:

        if not code:
            code = cls._generate_code(name)

        if SupplierRepository.code_exists(code):
            return ServiceResponse.validation_error(
                errors={"code": f"Supplier code '{code}' already exists"}
            )

        if rating is not None and (rating < 1 or rating > 5):
            return ServiceResponse.validation_error(
                errors={"rating": "Rating must be between 1 and 5"}
            )

        supplier = SupplierRepository.create(
            code=code,
            name=name,
            legal_name=legal_name,
            contact_person=contact_person,
            email=email,
            phone=phone,
            mobile=mobile,
            address=address,
            city=city,
            country=country,
            tax_id=tax_id,
            payment_terms_days=payment_terms_days,
            credit_limit=credit_limit,
            currency=currency,
            lead_time_days=lead_time_days,
            minimum_order_value=minimum_order_value,
            rating=rating,
            notes=notes,
        )

        return ServiceResponse.created(data={
            "id": supplier.id,
            "uuid": str(supplier.uuid),
            "code": supplier.code,
            "supplier": cls.serialize(supplier)
        }, message=f"Supplier '{name}' created")

    @classmethod
    def _generate_code(cls, name: str) -> str:
        prefix = "".join(c for c in name.upper() if c.isalnum())[:3]
        if len(prefix) < 3:
            prefix = prefix.ljust(3, "X")

        seq = SupplierRepository.get_next_code_seq(prefix)
        return f"{prefix}{seq:03d}"

    @classmethod
    @transaction.atomic
    def update(cls, supplier_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        supplier = SupplierRepository.get_by_id(supplier_id)
        if not supplier:
            return ServiceResponse.not_found(f"Supplier with id {supplier_id} not found")

        if "code" in kwargs and kwargs["code"] != supplier.code:
            if SupplierRepository.code_exists(kwargs["code"], exclude_id=supplier_id):
                return ServiceResponse.validation_error(
                    errors={"code": f"Supplier code '{kwargs['code']}' already exists"}
                )

        if "rating" in kwargs and kwargs["rating"] is not None:
            if kwargs["rating"] < 1 or kwargs["rating"] > 5:
                return ServiceResponse.validation_error(
                    errors={"rating": "Rating must be between 1 and 5"}
                )

        update_fields = ["updated_at"]
        allowed_fields = [
            "code", "name", "legal_name", "contact_person", "email",
            "phone", "mobile", "address", "city", "country", "tax_id",
            "payment_terms_days", "credit_limit", "currency",
            "lead_time_days", "minimum_order_value", "rating", "notes"
        ]

        for field in allowed_fields:
            if field in kwargs:
                setattr(supplier, field, kwargs[field])
                update_fields.append(field)

        supplier.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "supplier": cls.serialize(supplier)
        }, message="Supplier updated")

    @classmethod
    @transaction.atomic
    def deactivate(cls, supplier_id: int) -> Tuple[Dict[str, Any], int]:
        supplier = SupplierRepository.get_by_id(supplier_id)
        if not supplier:
            return ServiceResponse.not_found(f"Supplier with id {supplier_id} not found")

        if SupplierRepository.has_pending_orders(supplier):
            return ServiceResponse.error("Cannot deactivate supplier with pending purchase orders")

        supplier.is_active = False
        supplier.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "id": supplier_id
        }, message="Supplier deactivated")

    @classmethod
    @transaction.atomic
    def activate(cls, supplier_id: int) -> Tuple[Dict[str, Any], int]:
        supplier = SupplierRepository.get_by_id(supplier_id)
        if not supplier:
            return ServiceResponse.not_found(f"Supplier with id {supplier_id} not found")

        supplier.is_active = True
        supplier.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "supplier": cls.serialize(supplier)
        }, message="Supplier activated")

    @classmethod
    @transaction.atomic
    def update_balance(cls, supplier_id: int, amount: Decimal, operation: str = "add") -> Tuple[Dict[str, Any], int]:
        supplier = SupplierRepository.get_by_id(supplier_id)
        if not supplier:
            return ServiceResponse.not_found(f"Supplier with id {supplier_id} not found")

        amount = to_decimal(amount)

        if operation == "add":
            supplier.current_balance += amount
        elif operation == "subtract":
            supplier.current_balance -= amount
        elif operation == "set":
            supplier.current_balance = amount
        else:
            return ServiceResponse.validation_error(
                errors={"operation": "Invalid operation. Valid: add, subtract, set"}
            )

        supplier.save(update_fields=["current_balance", "updated_at"])

        return ServiceResponse.success(data={
            "current_balance": str(supplier.current_balance)
        }, message="Balance updated")


class SupplierStockItemService:

    @classmethod
    def serialize(cls, si: SupplierStockItem) -> Dict[str, Any]:
        return {
            "id": si.id,
            "uuid": str(si.uuid),
            "supplier_id": si.supplier_id,
            "stock_item_id": si.stock_item_id,
            "stock_item_name": si.stock_item.name,
            "supplier_sku": si.supplier_sku,
            "supplier_name": si.supplier_name,
            "unit_id": si.unit_id,
            "unit_name": si.unit.name,
            "unit_short": si.unit.short_name,
            "price": str(si.price),
            "currency": si.currency,
            "min_order_qty": str(si.min_order_qty),
            "pack_size": str(si.pack_size),
            "lead_time_days": si.lead_time_days,
            "is_preferred": si.is_preferred,
            "last_price_update": si.last_price_update.isoformat() if si.last_price_update else None,
            "notes": si.notes,
        }

    @classmethod
    @transaction.atomic
    def add_item(cls,
                 supplier_id: int,
                 stock_item_id: int,
                 unit_id: int,
                 price: Decimal,
                 supplier_sku: str = "",
                 supplier_name: str = "",
                 currency: str = "UZS",
                 min_order_qty: Decimal = Decimal("1"),
                 pack_size: Decimal = Decimal("1"),
                 lead_time_days: int = None,
                 is_preferred: bool = False,
                 notes: str = "") -> Tuple[Dict[str, Any], int]:

        supplier = SupplierRepository.first(id=supplier_id, is_active=True)
        if not supplier:
            return ServiceResponse.not_found(f"Supplier with id {supplier_id} not found")

        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            return ServiceResponse.not_found(f"Stock item with id {stock_item_id} not found")

        unit = StockUnitRepository.first(id=unit_id, is_active=True)
        if not unit:
            return ServiceResponse.not_found(f"Unit with id {unit_id} not found")

        if SupplierStockItemRepository.link_exists(supplier_id, stock_item_id):
            return ServiceResponse.validation_error(
                errors={"stock_item_id": "Item already exists for this supplier"}
            )

        if is_preferred:
            SupplierStockItemRepository.filter(
                stock_item_id=stock_item_id,
                is_preferred=True
            ).update(is_preferred=False)

        si = SupplierStockItemRepository.create(
            supplier_id=supplier_id,
            stock_item_id=stock_item_id,
            supplier_sku=supplier_sku,
            supplier_name=supplier_name or stock_item.name,
            unit=unit,
            price=to_decimal(price),
            currency=currency,
            min_order_qty=to_decimal(min_order_qty),
            pack_size=to_decimal(pack_size),
            lead_time_days=lead_time_days or supplier.lead_time_days,
            is_preferred=is_preferred,
            notes=notes,
            last_price_update=timezone.now(),
        )

        return ServiceResponse.created(data={
            "id": si.id,
            "supplier_item": cls.serialize(si)
        }, message="Item added to supplier")

    @classmethod
    @transaction.atomic
    def update_item(cls, supplier_item_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        si = SupplierStockItemRepository.get_by_id(supplier_item_id)
        if not si:
            return ServiceResponse.not_found(f"Supplier item with id {supplier_item_id} not found")

        update_fields = ["updated_at"]
        allowed_fields = [
            "supplier_sku", "supplier_name", "price", "currency",
            "min_order_qty", "pack_size", "lead_time_days", "notes"
        ]

        for field in allowed_fields:
            if field in kwargs:
                value = kwargs[field]
                if field in ["price", "min_order_qty", "pack_size"]:
                    value = to_decimal(value)
                setattr(si, field, value)
                update_fields.append(field)

        if "price" in kwargs:
            si.last_price_update = timezone.now()
            update_fields.append("last_price_update")

        if "is_preferred" in kwargs and kwargs["is_preferred"]:
            SupplierStockItemRepository.filter(
                stock_item_id=si.stock_item_id,
                is_preferred=True
            ).exclude(id=si.id).update(is_preferred=False)
            si.is_preferred = True
            update_fields.append("is_preferred")

        si.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "supplier_item": cls.serialize(si)
        }, message="Supplier item updated")

    @classmethod
    @transaction.atomic
    def remove_item(cls, supplier_item_id: int) -> Tuple[Dict[str, Any], int]:
        si = SupplierStockItemRepository.get_by_id(supplier_item_id)
        if not si:
            return ServiceResponse.not_found(f"Supplier item with id {supplier_item_id} not found")

        si.delete()

        return ServiceResponse.success(message="Item removed from supplier")

    @classmethod
    def get_preferred_supplier(cls, stock_item_id: int) -> Optional[SupplierStockItem]:
        return SupplierStockItemRepository.get_preferred(stock_item_id)

    @classmethod
    def get_cheapest_supplier(cls, stock_item_id: int) -> Optional[SupplierStockItem]:
        return SupplierStockItemRepository.get_cheapest(stock_item_id)
