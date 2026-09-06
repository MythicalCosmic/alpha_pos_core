"""Unit arithmetic and operation-scoped bulk conversion metadata."""

import logging
from decimal import Decimal

from base.helpers.response import ServiceResponse
from stock.models import StockItem, StockItemUnit, StockUnit
from stock.services.base_service import round_decimal, to_decimal

logger = logging.getLogger(__name__)


def convert_units(quantity, from_unit, to_unit):
    """Convert loaded unit objects using the public conversion response contract."""
    if from_unit.unit_type != to_unit.unit_type:
        return ServiceResponse.error(
            f"Cannot convert between different types: {from_unit.unit_type} -> {to_unit.unit_type}"
        )
    for unit in (from_unit, to_unit):
        if not unit.is_base_unit and (
            not unit.conversion_factor.is_finite() or unit.conversion_factor <= 0
        ):
            return ServiceResponse.error(
                "Unit conversion factor must be positive and finite"
            )
    quantity = to_decimal(quantity)
    base_quantity = (
        quantity if from_unit.is_base_unit else quantity * from_unit.conversion_factor
    )
    result = (
        base_quantity
        if to_unit.is_base_unit
        else base_quantity / to_unit.conversion_factor
    )
    result = round_decimal(result, to_unit.decimal_places)
    return result, {
        "from_quantity": str(quantity),
        "from_unit": from_unit.short_name,
        "to_quantity": str(result),
        "to_unit": to_unit.short_name,
        "base_quantity": str(round_decimal(base_quantity, 4)),
    }


class UnitConversions:
    """One operation's metadata; never a process-wide inventory/price cache."""

    def __init__(self, items, units):
        self.items = {item.pk: item for item in items}
        self.units = {unit.pk: unit for unit in units}
        for item in self.items.values():
            self.units[item.base_unit_id] = item.base_unit
        self.overrides = {
            (item_id, unit_id): factor
            for item_id, unit_id, factor in StockItemUnit.objects.filter(
                stock_item_id__in=self.items,
                unit_id__in=self.units,
                is_deleted=False,
            ).values_list("stock_item_id", "unit_id", "conversion_to_base")
        }

    @classmethod
    def for_ingredients(cls, ingredients):
        return cls(
            [line.stock_item for line in ingredients],
            [line.unit for line in ingredients],
        )

    @classmethod
    def for_requirements(cls, requirements):
        items = list(
            StockItem.objects.filter(
                pk__in={row["stock_item_id"] for row in requirements},
                is_deleted=False,
            ).select_related("base_unit")
        )
        units = StockUnit.objects.filter(
            pk__in={row.get("unit_id") for row in requirements if row.get("unit_id")},
            is_deleted=False,
        )
        return cls(items, units)

    def convert(self, stock_item_id, quantity, from_unit_id):
        quantity = to_decimal(quantity)
        # An omitted deduction unit already denotes the item's base unit.
        if from_unit_id is None:
            return quantity
        factor = self.overrides.get((stock_item_id, from_unit_id))
        if factor is not None:
            return quantity * factor
        item = self.items.get(stock_item_id)
        unit = self.units.get(from_unit_id)
        if item is not None and unit is not None:
            result, _ = convert_units(quantity, unit, item.base_unit)
            if isinstance(result, Decimal):
                return result
        # Retain the existing single-item missing-metadata fallback contract.
        logger.warning(
            "Unit conversion unavailable for stock item %s from unit %s; treating quantity as base units",
            stock_item_id,
            from_unit_id,
        )
        return quantity
