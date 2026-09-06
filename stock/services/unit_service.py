import logging
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
from django.db import transaction
from django.core.exceptions import ValidationError

from base.helpers.response import ServiceResponse
from stock.models import StockUnit, StockItemUnit
from stock.services.base_service import to_decimal, round_decimal
from stock.repositories import StockUnitRepository, StockItemUnitRepository

from stock.services.conversions import convert_units

logger = logging.getLogger(__name__)


def _valid_conversion_factor(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        factor = StockUnit._meta.get_field('conversion_factor').clean(value, None)
    except (ValidationError, TypeError, ValueError):
        return None
    return factor if factor.is_finite() and factor > 0 else None


def _factor_error():
    return ServiceResponse.validation_error(
        errors={'conversion_factor': 'Must be a positive finite number within 9 integer and 6 decimal digits'},
    )


class StockUnitService:

    @classmethod
    def serialize(cls, unit: StockUnit, include_derived: bool = False) -> Dict[str, Any]:
        data = {
            "id": unit.id,
            "uuid": str(unit.uuid),
            "name": unit.name,
            "short_name": unit.short_name,
            "unit_type": unit.unit_type,
            "unit_type_display": unit.get_unit_type_display(),
            "is_base_unit": unit.is_base_unit,
            "base_unit_id": unit.base_unit_id,
            "conversion_factor": str(unit.conversion_factor),
            "decimal_places": unit.decimal_places,
            "is_active": unit.is_active,
        }

        if unit.base_unit:
            data["base_unit"] = {
                "id": unit.base_unit.id,
                "name": unit.base_unit.name,
                "short_name": unit.base_unit.short_name,
            }

        if include_derived:
            derived = unit.derived_units.filter(is_active=True)
            data["derived_units"] = [
                {
                    "id": d.id,
                    "name": d.name,
                    "short_name": d.short_name,
                    "conversion_factor": str(d.conversion_factor),
                }
                for d in derived
            ]

        return data

    @classmethod
    def list(cls,
             include_inactive: bool = False,
             type_filter: str = None,
             base_only: bool = False) -> Tuple[Dict[str, Any], int]:
        if not include_inactive:
            queryset = StockUnitRepository.get_active()
        else:
            queryset = StockUnitRepository.get_all()

        if type_filter:
            queryset = queryset.filter(unit_type=type_filter)

        if base_only:
            queryset = queryset.filter(is_base_unit=True)

        queryset = queryset.order_by("unit_type", "name")

        units_by_type = {}
        all_units = []

        for unit in queryset:
            data = cls.serialize(unit)
            all_units.append(data)

            if unit.unit_type not in units_by_type:
                units_by_type[unit.unit_type] = []
            units_by_type[unit.unit_type].append(data)

        return ServiceResponse.success(data={
            "units": all_units,
            "by_type": units_by_type,
            "count": len(all_units),
            "types": [
                {"value": c[0], "label": c[1]}
                for c in StockUnit.UnitType.choices
            ]
        })

    @classmethod
    def get_by_type(cls, unit_type: str) -> Tuple[Dict[str, Any], int]:
        valid_types = [c[0] for c in StockUnit.UnitType.choices]
        if unit_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"unit_type": f"Invalid type. Valid: {valid_types}"}
            )

        units = StockUnitRepository.get_by_type(unit_type).order_by("-is_base_unit", "name")

        base_unit = units.filter(is_base_unit=True).first()

        return ServiceResponse.success(data={
            "units": [cls.serialize(u) for u in units],
            "base_unit": cls.serialize(base_unit) if base_unit else None,
            "count": units.count()
        })

    @classmethod
    def search(cls, query: str, limit: int = 20) -> Tuple[Dict[str, Any], int]:
        queryset = StockUnitRepository.get_active()
        units = StockUnitRepository.search(queryset, query).order_by("name")[:limit]

        return ServiceResponse.success(data={
            "units": [cls.serialize(u) for u in units],
            "count": len(units)
        })

    @classmethod
    def get(cls, unit_id: int, include_derived: bool = True) -> Tuple[Dict[str, Any], int]:
        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found(f"Unit with id {unit_id} not found")

        return ServiceResponse.success(data={
            "unit": cls.serialize(unit, include_derived=include_derived)
        })

    @classmethod
    def get_base_unit(cls, unit_type: str) -> Optional[StockUnit]:
        return StockUnitRepository.get_base_units(unit_type).first()

    @classmethod
    @transaction.atomic
    def create(cls,
               name: str,
               short_name: str,
               unit_type: str,
               is_base_unit: bool = False,
               base_unit_id: int = None,
               conversion_factor: Decimal = Decimal("1"),
               decimal_places: int = 2) -> Tuple[Dict[str, Any], int]:

        valid_types = [c[0] for c in StockUnit.UnitType.choices]
        if unit_type not in valid_types:
            return ServiceResponse.validation_error(
                errors={"unit_type": f"Invalid type. Valid: {valid_types}"}
            )

        if StockUnitRepository.short_name_exists(short_name):
            return ServiceResponse.validation_error(
                errors={"short_name": f"Unit with short name '{short_name}' already exists"}
            )

        base_unit = None
        if is_base_unit:
            existing_base = cls.get_base_unit(unit_type)
            if existing_base:
                return ServiceResponse.error(
                    f"Base unit already exists for {unit_type}: {existing_base.name}"
                )
            conversion_factor = Decimal("1")
        elif base_unit_id:
            base_unit = StockUnitRepository.get_by_id(base_unit_id)
            if not base_unit:
                return ServiceResponse.not_found(f"Base unit with id {base_unit_id} not found")
            if base_unit.unit_type != unit_type:
                return ServiceResponse.validation_error(
                    errors={"base_unit_id": "Base unit must be of same type"}
                )
            if not base_unit.is_base_unit:
                return ServiceResponse.validation_error(
                    errors={"base_unit_id": "Referenced unit is not a base unit"}
                )
        else:
            base_unit = cls.get_base_unit(unit_type)
            if not base_unit:
                return ServiceResponse.error(
                    f"No base unit exists for {unit_type}. Create base unit first."
                )

        conversion_factor = _valid_conversion_factor(conversion_factor)
        if conversion_factor is None:
            return _factor_error()

        unit = StockUnitRepository.create(
            name=name,
            short_name=short_name,
            unit_type=unit_type,
            is_base_unit=is_base_unit,
            base_unit=base_unit,
            conversion_factor=to_decimal(conversion_factor),
            decimal_places=decimal_places,
        )

        return ServiceResponse.success(data={
            "id": unit.id,
            "uuid": str(unit.uuid),
            "unit": unit.name,
        }, message=f"Unit '{name}' created")

    @classmethod
    @transaction.atomic
    def update(cls, unit_id: int, **kwargs) -> Tuple[Dict[str, Any], int]:
        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found(f"Unit with id {unit_id} not found")

        if "short_name" in kwargs and kwargs["short_name"] != unit.short_name:
            if StockUnitRepository.short_name_exists(kwargs["short_name"], exclude_id=unit_id):
                return ServiceResponse.validation_error(
                    errors={"short_name": f"Unit with short name '{kwargs['short_name']}' already exists"}
                )

        if "unit_type" in kwargs and kwargs["unit_type"] != unit.unit_type:
            if unit.is_base_unit and StockUnitRepository.has_derived_units(unit):
                return ServiceResponse.error("Cannot change type of base unit with derived units")

        if 'conversion_factor' in kwargs:
            factor = _valid_conversion_factor(kwargs['conversion_factor'])
            if factor is None:
                return _factor_error()
            kwargs['conversion_factor'] = factor

        update_fields = []
        for field in ["name", "short_name", "conversion_factor", "decimal_places"]:
            if field in kwargs:
                setattr(unit, field, kwargs[field])
                update_fields.append(field)

        if update_fields:
            unit.save(update_fields=update_fields)

        return ServiceResponse.success(data={
            "unit": cls.serialize(unit)
        }, message="Unit updated")

    @classmethod
    @transaction.atomic
    def deactivate(cls, unit_id: int) -> Tuple[Dict[str, Any], int]:
        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found(f"Unit with id {unit_id} not found")

        if StockUnitRepository.has_stock_items(unit):
            return ServiceResponse.error("Cannot deactivate unit used by stock items")

        if unit.is_base_unit and unit.derived_units.filter(is_active=True).exists():
            return ServiceResponse.error("Cannot deactivate base unit with active derived units")

        unit.is_active = False
        unit.save(update_fields=["is_active"])

        return ServiceResponse.success(data={
            "id": unit_id
        }, message="Unit deactivated")

    @classmethod
    def convert(cls,
                quantity: Decimal,
                from_unit_id: int,
                to_unit_id: int) -> Tuple[Decimal, Dict[str, Any]]:

        from_unit = StockUnitRepository.get_by_id(from_unit_id)
        to_unit = StockUnitRepository.get_by_id(to_unit_id)

        if not from_unit:
            return ServiceResponse.not_found(f"From unit with id {from_unit_id} not found")
        if not to_unit:
            return ServiceResponse.not_found(f"To unit with id {to_unit_id} not found")

        return convert_units(quantity, from_unit, to_unit)

    @classmethod
    def to_base(cls, quantity: Decimal, unit_id: int):
        unit = StockUnitRepository.get_by_id(unit_id)
        if not unit:
            return ServiceResponse.not_found(f"Unit with id {unit_id} not found")

        quantity = to_decimal(quantity)

        if unit.is_base_unit:
            return quantity, unit

        base_quantity = quantity * unit.conversion_factor
        base_unit = unit.base_unit if unit.base_unit else unit

        return round_decimal(base_quantity, 4), base_unit

    @classmethod
    def from_base(cls, base_quantity: Decimal, to_unit_id: int):
        unit = StockUnitRepository.get_by_id(to_unit_id)
        if not unit:
            return ServiceResponse.not_found(f"Unit with id {to_unit_id} not found")

        base_quantity = to_decimal(base_quantity)

        if unit.is_base_unit:
            return base_quantity

        result = base_quantity / unit.conversion_factor
        return round_decimal(result, unit.decimal_places)


class StockItemUnitService:

    @classmethod
    def serialize(cls, item_unit: StockItemUnit) -> Dict[str, Any]:
        return {
            "id": item_unit.id,
            "uuid": str(item_unit.uuid),
            "stock_item_id": item_unit.stock_item_id,
            "unit_id": item_unit.unit_id,
            "unit": {
                "id": item_unit.unit.id,
                "name": item_unit.unit.name,
                "short_name": item_unit.unit.short_name,
            },
            "is_default": item_unit.is_default,
            "conversion_to_base": str(item_unit.conversion_to_base),
            "barcode": item_unit.barcode,
        }

    @classmethod
    def get_for_item(cls, stock_item_id: int) -> Tuple[Dict[str, Any], int]:
        item_units = StockItemUnitRepository.get_for_item(
            stock_item_id
        ).order_by("-is_default", "unit__name")

        return ServiceResponse.success(data={
            "units": [cls.serialize(iu) for iu in item_units],
            "count": item_units.count()
        })


    @classmethod
    def convert_for_item(cls,
                         stock_item_id: int,
                         quantity: Decimal,
                         from_unit_id: int) -> Decimal:
        # CONTRACT: this MUST always return a Decimal. Callers (e.g.
        # StockLevelService.adjust) do arithmetic on the result mid-transaction,
        # so returning a ServiceResponse (dict, int) tuple here would blow up
        # with a TypeError. On any missing/inconsistent data we fall back to the
        # raw quantity treated as base units and log a warning instead.
        quantity = to_decimal(quantity)

        item_unit = StockItemUnitRepository.get_by_item_and_unit(stock_item_id, from_unit_id)
        if item_unit:
            return quantity * item_unit.conversion_to_base

        from stock.repositories import StockItemRepository
        stock_item = StockItemRepository.get_by_id(stock_item_id)
        if not stock_item:
            logger.warning(
                "convert_for_item: stock item %s not found; treating quantity "
                "as base units", stock_item_id,
            )
            return quantity

        result = StockUnitService.convert(
            quantity,
            from_unit_id,
            stock_item.base_unit_id
        )
        # convert() returns (Decimal, details) on success but a ServiceResponse
        # (dict, int) tuple on failure (unknown unit, mismatched unit types).
        # Distinguish them: a successful first element is a Decimal.
        converted = result[0]
        if not isinstance(converted, Decimal):
            logger.warning(
                "convert_for_item: unit conversion failed for item %s from "
                "unit %s; treating quantity as base units", stock_item_id,
                from_unit_id,
            )
            return quantity
        return converted
