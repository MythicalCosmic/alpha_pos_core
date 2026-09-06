from decimal import Decimal

import pytest

from stock.models import StockUnit
from stock.services.unit_service import StockUnitService


@pytest.mark.parametrize(
    "factor", [0, -1, True, None, "NaN", "Infinity", "abc", "1000000000", "0.0000001"]
)
@pytest.mark.parametrize("operation", ["create", "update"])
def test_unit_factor_rejects_invalid_or_unstorable_values(
    inventory_catalog, factor, operation
):
    c = inventory_catalog
    before = list(StockUnit.objects.order_by("id").values())
    if operation == "create":
        result, status = StockUnitService.create(
            name="Rejected unit",
            short_name="bad",
            unit_type="WEIGHT",
            base_unit_id=c.gram.id,
            conversion_factor=factor,
        )
    else:
        result, status = StockUnitService.update(
            c.kilogram.id, conversion_factor=factor
        )
    assert status == 422, result
    assert list(StockUnit.objects.order_by("id").values()) == before


def test_valid_unit_factor_preserves_conversion(inventory_catalog):
    c = inventory_catalog
    result, status = StockUnitService.update(c.kilogram.id, conversion_factor="250.5")
    assert status == 200, result
    value, details = StockUnitService.convert(Decimal("2"), c.kilogram.id, c.gram.id)
    assert value == Decimal("501")
