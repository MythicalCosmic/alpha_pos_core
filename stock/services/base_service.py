from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import timedelta
from django.utils import timezone

from base.services.sequence import generate_number

__all__ = ("generate_number", "get_date_range", "round_decimal", "to_decimal", "validated_quantity")


def validated_quantity(value, *, allow_zero=False):
    """Validate document quantities before they can reach inventory arithmetic."""
    try:
        if isinstance(value, bool):
            raise ValueError
        quantity = Decimal(str(value))
        if not quantity.is_finite() or quantity < 0 or (quantity == 0 and not allow_zero):
            raise ValueError
        if quantity > Decimal('99999999999.9999'):
            raise ValueError
        if quantity != quantity.quantize(Decimal('0.0001')):
            raise ValueError
    except (ValueError, TypeError, InvalidOperation) as exc:
        boundary = 'non-negative' if allow_zero else 'positive'
        raise ValueError(f'Quantity must be finite, {boundary}, and have at most 4 decimals.') from exc
    return quantity


def to_decimal(value, default=Decimal("0")):
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def round_decimal(value, places=4):
    if value is None:
        return Decimal("0")
    quantize_str = "0." + "0" * places
    return value.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP)


def get_date_range(period):
    today = timezone.localdate()

    if period == "today":
        return today, today
    elif period == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    elif period == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today
    elif period == "last_week":
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
        return start, end
    elif period == "this_month":
        return today.replace(day=1), today
    elif period == "last_month":
        first_of_month = today.replace(day=1)
        last_month_end = first_of_month - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        return last_month_start, last_month_end
    elif period == "this_year":
        return today.replace(month=1, day=1), today
    elif period.startswith("last_") and period.endswith("_days"):
        # Only catch the narrow ValueError from int(). A bare except hid
        # every typo (e.g. "last_abc_days") behind a silent fallback to
        # the today/today range, which is invisible to analytics callers.
        try:
            days = int(period.replace("last_", "").replace("_days", ""))
        except ValueError:
            return today, today
        return today - timedelta(days=days), today

    return today, today
