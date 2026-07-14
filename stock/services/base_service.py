from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.utils import timezone

from base.services.sequence import generate_number


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
