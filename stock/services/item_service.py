from typing import Dict, Any, Tuple
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum, F

from base.helpers.response import ServiceResponse
from stock.models import StockItem
from stock.services.base_service import to_decimal, round_decimal
from stock.repositories import (
    StockItemRepository, StockCategoryRepository,
    StockUnitRepository, StockLevelRepository,
    StockItemUnitRepository,
)


class StockItemService:

    @classmethod
    def serialize(cls, item: StockItem,
                  include_levels: bool = False,
                  include_units: bool = False,
                  include_suppliers: bool = False,
                  location_id: int = None) -> Dict[str, Any]:
        data = {
            "id": item.id,
            "uuid": str(item.uuid),
            "name": item.name,
            "sku": item.sku,
            "barcode": item.barcode,
            "item_type": item.item_type,
            "item_type_display": item.get_item_type_display(),

            "category_id": item.category_id,
            "category": {
                "id": item.category.id,
                "name": item.category.name,
            } if item.category else None,

            "base_unit_id": item.base_unit_id,
            "base_unit": {
                "id": item.base_unit.id,
                "name": item.base_unit.name,
                "short_name": item.base_unit.short_name,
            },

            "min_stock_level": str(item.min_stock_level),
            "max_stock_level": str(item.max_stock_level) if item.max_stock_level else None,
            "reorder_point": str(item.reorder_point),

            "cost_price": str(item.cost_price),
            "avg_cost_price": str(item.avg_cost_price),
            "last_cost_price": str(item.last_cost_price),

            "is_purchasable": item.is_purchasable,
            "is_sellable": item.is_sellable,
            "is_producible": item.is_producible,
            "track_batches": item.track_batches,
            "track_expiry": item.track_expiry,
            "default_expiry_days": item.default_expiry_days,
            "storage_conditions": item.storage_conditions,

            "is_active": item.is_active,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }

        if include_levels:
            levels_query = StockLevelRepository.filter(stock_item=item)
            if location_id:
                levels_query = levels_query.filter(location_id=location_id)

            data["stock_levels"] = [
                {
                    "location_id": lvl.location_id,
                    "location_name": lvl.location.name,
                    "quantity": str(lvl.quantity),
                    "reserved": str(lvl.reserved_quantity),
                    "available": str(lvl.available_quantity),
                    "pending_in": str(lvl.pending_in_quantity),
                    "pending_out": str(lvl.pending_out_quantity),
                }
                for lvl in levels_query.select_related("location")
            ]

            totals = levels_query.aggregate(
                total=Sum("quantity"),
                reserved=Sum("reserved_quantity")
            )
            data["total_stock"] = str(totals["total"] or 0)
            data["total_reserved"] = str(totals["reserved"] or 0)

        if include_units:
            data["alternative_units"] = [
                {
                    "id": au.id,
                    "unit_id": au.unit_id,
                    "unit_name": au.unit.name,
                    "short_name": au.unit.short_name,
                    "conversion_to_base": str(au.conversion_to_base),
                    "is_default": au.is_default,
                    "barcode": au.barcode,
                }
                for au in StockItemUnitRepository.get_for_item(item.id)
            ]

        if include_suppliers:
            data["suppliers"] = [
                {
                    "supplier_id": si.supplier_id,
                    "supplier_name": si.supplier.name,
                    "supplier_sku": si.supplier_sku,
                    "price": str(si.price),
                    "currency": si.currency,
                    "min_order_qty": str(si.min_order_qty),
                    "is_preferred": si.is_preferred,
                    "lead_time_days": si.lead_time_days,
                }
                for si in item.suppliers.select_related("supplier")
            ]

        return data

    @classmethod
    def serialize_brief(cls, item: StockItem) -> Dict[str, Any]:
        return {
            "id": item.id,
            "uuid": str(item.uuid),
            "name": item.name,
            "sku": item.sku,
            "item_type": item.item_type,
            "category_id": item.category_id,
            "base_unit_short": item.base_unit.short_name,
            "is_active": item.is_active,
        }

    @classmethod
    def list(cls,
             page: int = 1,
             per_page: int = 20,
             search: str = None,
             category_id: int = None,
             item_type: str = None,
             active_only: bool = True,
             purchasable_only: bool = False,
             sellable_only: bool = False,
             producible_only: bool = False,
             is_purchasable: bool = False,
             is_sellable: bool = False,
             is_producible: bool = False,
             low_stock_only: bool = False,
             low_stock: bool = False,
             location_id: int = None,
             include_levels: bool = False) -> Tuple[Dict[str, Any], int]:

        if active_only:
            queryset = StockItemRepository.get_active()
        else:
            queryset = StockItemRepository.get_all()

        queryset = queryset.select_related("category", "base_unit")

        if search:
            queryset = StockItemRepository.search(queryset, search)

        if category_id:
            queryset = queryset.filter(category_id=category_id)

        if item_type:
            valid_types = [c[0] for c in StockItem.ItemType.choices]
            if item_type not in valid_types:
                return ServiceResponse.validation_error(
                    errors={"item_type": f"Invalid type. Valid: {valid_types}"}
                )
            queryset = queryset.filter(item_type=item_type)

        if purchasable_only:
            queryset = queryset.filter(is_purchasable=True)

        if sellable_only:
            queryset = queryset.filter(is_sellable=True)

        if producible_only:
            queryset = queryset.filter(is_producible=True)

        if low_stock:
            queryset = queryset.annotate(
                total_qty=Sum("stock_levels__quantity")
            ).filter(
                Q(total_qty__lt=F("reorder_point")) |
                Q(total_qty__isnull=True)
            )

        queryset = queryset.order_by("name")

        page_obj, paginator = StockItemRepository.paginate(queryset, page, per_page)

        return ServiceResponse.success(data={
            "items": [
                cls.serialize(item, include_levels=include_levels, location_id=location_id)
                for item in page_obj.object_list
            ],
            "pagination": {
                "current_page": page_obj.number,
                "total_pages": paginator.num_pages,
                "total_items": paginator.count,
                "per_page": per_page,
                "has_next": page_obj.has_next(),
                "has_previous": page_obj.has_previous(),
            },
            "filters": {
                "types": [{"value": c[0], "label": c[1]} for c in StockItem.ItemType.choices]
            }
        })

    @classmethod
    def search(cls, query: str, limit: int = 20,
               item_type: str = None,
               purchasable_only: bool = False) -> Tuple[Dict[str, Any], int]:
        queryset = StockItemRepository.search_exact(query)

        if item_type:
            queryset = queryset.filter(item_type=item_type)

        if purchasable_only:
            queryset = queryset.filter(is_purchasable=True)

        items = list(queryset.order_by("name")[:limit])

        return ServiceResponse.success(data={
            "items": [cls.serialize_brief(item) for item in items],
            "count": len(items),
        })

    @classmethod
    def find_by_barcode(cls, barcode: str) -> Tuple[Dict[str, Any], int]:
        item = StockItemRepository.get_by_barcode(barcode)

        if item:
            return ServiceResponse.success(data={
                "item": cls.serialize(item, include_levels=True),
                "unit_id": item.base_unit_id,
                "conversion": "1",
            })

        item_unit = StockItemUnitRepository.first(
            barcode=barcode,
            stock_item__is_active=True,
        )

        if item_unit:
            return ServiceResponse.success(data={
                "item": cls.serialize(item_unit.stock_item, include_levels=True),
                "unit_id": item_unit.unit_id,
                "conversion": str(item_unit.conversion_to_base),
            })

        return ServiceResponse.not_found(f"Item with barcode '{barcode}' not found")

    @classmethod
    def get(cls, item_id: int,
            include_levels: bool = True,
            include_units: bool = True,
            include_suppliers: bool = True) -> Tuple[Dict[str, Any], int]:
        item = StockItemRepository.get_with_relations(item_id)
        if not item:
            return ServiceResponse.not_found(f"Stock item with id {item_id} not found")

        return ServiceResponse.success(data={
            "item": cls.serialize(
                item,
                include_levels=include_levels,
                include_units=include_units,
                include_suppliers=include_suppliers
            )
        })

    @classmethod
    @transaction.atomic
    def create(cls,
               name: str,
               base_unit_id: int,
               item_type: str = "RAW",
               category_id: int = None,
               sku: str = None,
               barcode: str = None,
               min_stock_level: Decimal = Decimal("0"),
               max_stock_level: Decimal = None,
               reorder_point: Decimal = Decimal("0"),
               cost_price: Decimal = Decimal("0"),
               is_purchasable: bool = True,
               is_sellable: bool = False,
               is_producible: bool = False,
               track_batches: bool = False,
               track_expiry: bool = False,
               default_expiry_days: int = None,
               storage_conditions: str = "",
               initial_stock: Decimal = None,
               initial_location_id: int = None) -> Tuple[Dict[str, Any], int]:

        valid_types = [c[0] for c in StockItem.ItemType.choices]
        if item_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"item_type": f"Invalid type. Valid: {valid_types}"}
            )

        base_unit = StockUnitRepository.first(id=base_unit_id, is_active=True)
        if not base_unit:
            return ServiceResponse.not_found(f"Base unit with id {base_unit_id} not found")

        category = None
        if category_id:
            category = StockCategoryRepository.first(id=category_id, is_active=True)
            if not category:
                return ServiceResponse.not_found(f"Category with id {category_id} not found")

        if sku:
            if StockItemRepository.sku_exists(sku):
                return ServiceResponse.validation_error(
                    errors={"sku": f"SKU '{sku}' already exists"}
                )

        if barcode:
            if StockItemRepository.barcode_exists(barcode):
                return ServiceResponse.validation_error(
                    errors={"barcode": f"Barcode '{barcode}' already exists"}
                )

        if not sku:
            sku = cls._generate_sku(name, item_type)

        item = StockItemRepository.create(
            name=name,
            base_unit=base_unit,
            item_type=item_type,
            category=category,
            sku=sku,
            barcode=barcode,
            min_stock_level=to_decimal(min_stock_level),
            max_stock_level=to_decimal(max_stock_level) if max_stock_level else None,
            reorder_point=to_decimal(reorder_point),
            cost_price=to_decimal(cost_price),
            avg_cost_price=to_decimal(cost_price),
            last_cost_price=to_decimal(cost_price),
            is_purchasable=is_purchasable,
            is_sellable=is_sellable,
            is_producible=is_producible,
            track_batches=track_batches,
            track_expiry=track_expiry,
            default_expiry_days=default_expiry_days,
            storage_conditions=storage_conditions,
        )

        if initial_stock and to_decimal(initial_stock) > 0:
            from stock.services.level_service import StockLevelService
            location_id = initial_location_id
            if not location_id:
                from .settings_service import StockSettingsService
                location_id = StockSettingsService.get_default_location_id()

            if location_id:
                StockLevelService.adjust(
                    stock_item_id=item.id,
                    location_id=location_id,
                    quantity=to_decimal(initial_stock),
                    movement_type="OPENING_BALANCE",
                    user_id=1,  # System user
                    notes="Initial stock on item creation"
                )

        return ServiceResponse.created(data={
            "id": item.id,
            "uuid": str(item.uuid),
            "sku": item.sku,
            "item": cls.serialize(item)
        }, message=f"Stock item '{name}' created")

    @classmethod
    def _generate_sku(cls, name: str, item_type: str) -> str:
        prefix = item_type[:3].upper()
        name_part = "".join(c for c in name.upper() if c.isalnum())[:3]

        existing = StockItemRepository.count(
            sku__startswith=f"{prefix}-{name_part}"
        )

        return f"{prefix}-{name_part}-{existing + 1:04d}"

    @classmethod
    @transaction.atomic
    def update(cls, item_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        item = StockItemRepository.get_by_id(item_id)
        if not item:
            return ServiceResponse.not_found(f"Stock item with id {item_id} not found")

        if "item_type" in kwargs:
            valid_types = [c[0] for c in StockItem.ItemType.choices]
            if kwargs["item_type"] not in valid_types:
                return ServiceResponse.validation_error(
                    errors={"item_type": f"Invalid type. Valid: {valid_types}"}
                )

        if "category_id" in kwargs:
            if kwargs["category_id"]:
                category = StockCategoryRepository.first(id=kwargs["category_id"], is_active=True)
                if not category:
                    return ServiceResponse.not_found(
                        f"Category with id {kwargs['category_id']} not found"
                    )
                item.category = category
            else:
                item.category = None

        if "base_unit_id" in kwargs:
            base_unit = StockUnitRepository.first(id=kwargs["base_unit_id"], is_active=True)
            if not base_unit:
                return ServiceResponse.not_found(
                    f"Base unit with id {kwargs['base_unit_id']} not found"
                )
            if StockItemRepository.has_transactions(item):
                return ServiceResponse.error(
                    "Cannot change base unit for item with transactions"
                )
            item.base_unit = base_unit

        if "sku" in kwargs and kwargs["sku"] != item.sku:
            if StockItemRepository.sku_exists(kwargs["sku"], exclude_id=item_id):
                return ServiceResponse.validation_error(
                    errors={"sku": f"SKU '{kwargs['sku']}' already exists"}
                )

        if "barcode" in kwargs and kwargs["barcode"] != item.barcode:
            if kwargs["barcode"] and StockItemRepository.barcode_exists(kwargs["barcode"], exclude_id=item_id):
                return ServiceResponse.validation_error(
                    errors={"barcode": f"Barcode '{kwargs['barcode']}' already exists"}
                )

        update_fields = ["updated_at"]
        direct_fields = [
            "name", "sku", "barcode", "item_type",
            "min_stock_level", "max_stock_level", "reorder_point",
            "cost_price", "is_purchasable", "is_sellable", "is_producible",
            "track_batches", "track_expiry", "default_expiry_days", "storage_conditions"
        ]

        for field in direct_fields:
            if field in kwargs:
                value = kwargs[field]
                if field in ["min_stock_level", "max_stock_level", "reorder_point", "cost_price"]:
                    value = to_decimal(value) if value is not None else None
                setattr(item, field, value)
                update_fields.append(field)

        if "category_id" in kwargs:
            update_fields.append("category")
        if "base_unit_id" in kwargs:
            update_fields.append("base_unit")

        item.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "item": cls.serialize(item, include_levels=True)
        }, message="Stock item updated")

    @classmethod
    @transaction.atomic
    def update_cost(cls, item_id: int, new_cost: Decimal,
                    update_type: str = "LAST",
                    received_qty: Decimal = None) -> Tuple[Dict[str, Any], int]:
        # Lock the item row: the AVG branch is a read-modify-write of
        # avg_cost_price. Two concurrent receipts of the same item would each
        # read the old average and the second save() would clobber the first,
        # corrupting the cost basis. select_for_update serializes them.
        item = StockItemRepository.get_for_update(item_id)
        if not item:
            return ServiceResponse.not_found(f"Stock item with id {item_id} not found")

        new_cost = to_decimal(new_cost)
        update_fields = ["updated_at", "last_cost_price"]
        item.last_cost_price = new_cost

        if update_type == "ALL":
            item.cost_price = new_cost
            item.avg_cost_price = new_cost
            update_fields.extend(["cost_price", "avg_cost_price"])
        elif update_type == "AVG":
            recv_qty = to_decimal(received_qty) if received_qty is not None else None
            total_qty = to_decimal(StockLevelRepository.get_total_quantity(item.id))

            # total_qty includes the just-received quantity (caller adjusts levels first).
            # Old qty before this receipt = total_qty - recv_qty.
            if recv_qty is not None and recv_qty > 0 and total_qty >= recv_qty:
                old_qty = total_qty - recv_qty
                if old_qty > 0:
                    new_avg = (old_qty * item.avg_cost_price + recv_qty * new_cost) / total_qty
                else:
                    new_avg = new_cost
                item.avg_cost_price = round_decimal(new_avg, 4)
            elif total_qty > 0:
                item.avg_cost_price = round_decimal(new_cost, 4)
            else:
                item.avg_cost_price = new_cost
            update_fields.append("avg_cost_price")

        item.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "cost_price": str(item.cost_price),
            "avg_cost_price": str(item.avg_cost_price),
            "last_cost_price": str(item.last_cost_price),
        }, message="Cost updated")

    @classmethod
    @transaction.atomic
    def deactivate(cls, item_id: int, force: bool = False) -> Tuple[Dict[str, Any], int]:
        item = StockItemRepository.get_by_id(item_id)
        if not item:
            return ServiceResponse.not_found(f"Stock item with id {item_id} not found")

        if not force:
            total_stock = StockLevelRepository.get_total_quantity(item.id)

            if total_stock > 0:
                return ServiceResponse.error(
                    f"Cannot deactivate item with {total_stock} in stock. "
                    "Adjust stock to zero first or use force=True."
                )

        item.is_active = False
        item.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "id": item_id
        }, message="Stock item deactivated")

    @classmethod
    @transaction.atomic
    def activate(cls, item_id: int) -> Tuple[Dict[str, Any], int]:
        item = StockItemRepository.get_by_id(item_id)
        if not item:
            return ServiceResponse.not_found(f"Stock item with id {item_id} not found")

        item.is_active = True
        item.save(update_fields=["is_active", "updated_at"])

        return ServiceResponse.success(data={
            "item": cls.serialize(item)
        }, message="Stock item activated")

    @classmethod
    def get_stats(cls) -> Tuple[Dict[str, Any], int]:
        stats = StockItemRepository.get_stats()

        low_stock_count = StockItemRepository.get_low_stock().count()

        no_category_count = StockItemRepository.count(
            is_active=True,
            category__isnull=True,
        )

        return ServiceResponse.success(data={
            "total_items": stats["active"],
            "by_type": stats["by_type"],
            "low_stock_count": low_stock_count,
            "no_category_count": no_category_count,
        })
