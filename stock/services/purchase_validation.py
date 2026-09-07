"""Validated procurement values shared by planned and direct receiving."""

from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from base.money import MoneyValueError, decimal_value
from stock.models import StockItemUnit, StockUnit, SupplierStockItem

MAX_QUANTITY = Decimal("99999999999.9999")
MAX_VALUE = Decimal("99999999999.9999")


def purchase_date(value, field, *, optional=False):
    if optional and value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise MoneyValueError(f"{field} must be a valid YYYY-MM-DD date") from exc


def purchase_datetime(value, field):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date) or (isinstance(value, str) and len(value) == 10):
        parsed = datetime.combine(purchase_date(value, field), datetime.min.time())
    else:
        try:
            parsed = parse_datetime(value)
        except (TypeError, ValueError):
            parsed = None
    if parsed is None:
        raise MoneyValueError(f"{field} must be a valid ISO date or datetime")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def valid_purchase_values(
    quantity, price, discount=0, tax=0, *, places=4, allow_free=False
):
    if isinstance(quantity, Decimal):
        quantity = format(quantity.normalize(), "f")
    quantity = decimal_value(
        quantity, "quantity", places=min(places, 4), positive=True, maximum=MAX_QUANTITY
    )
    price = decimal_value(
        price, "unit_price", places=4, positive=not allow_free, maximum=MAX_VALUE
    )
    discount = decimal_value(discount, "discount_percent", places=2, maximum=100)
    tax = decimal_value(tax, "tax_percent", places=2, maximum=100)
    total = quantity * price * (1 - discount / 100)
    if total > MAX_VALUE:
        raise MoneyValueError("Line total exceeds the supported inventory value")
    return quantity, price, discount, tax, total


def unit_factor(item, unit, *, overrides=None):
    """Only actual unit conversions apply; supplier pack_size is never used."""
    if (
        not unit
        or not unit.is_active
        or unit.is_deleted
        or not item.base_unit.is_active
        or item.base_unit.is_deleted
    ):
        return None
    if unit.id == item.base_unit_id:
        return Decimal("1")
    if overrides is None:
        overrides = dict(
            StockItemUnit.objects.filter(
                stock_item=item,
                unit=unit,
                branch_id=item.branch_id,
                is_deleted=False,
            ).values_list("unit_id", "conversion_to_base")
        )
    factor = overrides.get(unit.id)
    if factor is None and unit.base_unit_id == item.base_unit_id:
        factor = unit.conversion_factor
    if factor is None:
        return None
    factor = Decimal(factor)
    return factor if factor.is_finite() and factor > 0 else None


def validate_purchase_catalog(po, item, unit_id, supplier_item_id=None):
    if (
        not item
        or item.is_deleted
        or not item.is_active
        or not item.is_purchasable
        or item.branch_id != po.branch_id
    ):
        raise MoneyValueError("A live purchasable item in this branch is required")
    unit = StockUnit.objects.filter(
        id=unit_id, is_deleted=False, is_active=True
    ).first()
    if unit_factor(item, unit) is None:
        raise MoneyValueError("A compatible active purchase unit is required")
    if supplier_item_id is not None:
        link = SupplierStockItem.objects.filter(
            id=supplier_item_id,
            supplier_id=po.supplier_id,
            stock_item=item,
            unit=unit,
            branch_id=po.branch_id,
            is_deleted=False,
            is_active=True,
        ).first()
        if not link:
            raise MoneyValueError(
                "Supplier item does not match the supplier, item, unit, and branch"
            )
    return unit
