from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.utils import timezone


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


def _max_existing_seq(model_class, field, scope):
    """Highest sequence number already used for `scope` (prefix-date), or 0.

    Lets the counter seed itself on first use so it never re-issues a number
    that pre-dates the counter (e.g. the transition day, or rows created by an
    older build). Cheap, indexed startswith lookup; runs only when the counter
    row for the scope doesn't exist yet.
    """
    last = (
        model_class.objects
        .filter(**{f"{field}__startswith": f"{scope}-"})
        .order_by(f"-{field}")
        .first()
    )
    if not last:
        return 0
    try:
        return int(getattr(last, field).split("-")[-1])
    except (ValueError, AttributeError):
        return 0


def generate_number(prefix, model_class, field="order_number"):
    """Allocate the next `PREFIX-YYYYMMDD-NNNN` document number atomically.

    Was a read-max-then-+1 with no lock: two concurrent creates computed the
    same NNNN, and the second insert violated the unique constraint and aborted
    its enclosing operation (notably the sale's stock deduction). Now backed by
    a select_for_update-locked SequenceCounter row, mirroring DisplayIdCounter.
    """
    from django.db import transaction
    from base.models import SequenceCounter

    today = timezone.now()
    scope = f"{prefix}-{today.strftime('%Y%m%d')}"

    with transaction.atomic():
        row, created = (
            SequenceCounter.objects
            .select_for_update()
            .get_or_create(
                scope=scope,
                defaults={"value": _max_existing_seq(model_class, field, scope)},
            )
        )
        row.value = row.value + 1
        row.save(update_fields=["value", "updated_at"])
        return f"{scope}-{row.value:04d}"


def get_date_range(period):
    today = timezone.now().date()

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
