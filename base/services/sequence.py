"""Atomic allocation for human-readable, date-scoped document numbers."""

from django.db import transaction
from django.utils import timezone

from base.models import SequenceCounter


def _max_existing_seq(model_class, field, scope):
    """Return the highest legacy suffix for ``scope`` before a counter exists."""
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
    """Allocate ``PREFIX-YYYYMMDD-NNNN`` under a database row lock.

    The counter seeds itself from legacy rows on first use. This prevents two
    concurrent requests from reading the same current maximum and attempting
    the same unique document number.
    """
    today = timezone.localdate()
    scope = f"{prefix}-{today.strftime('%Y%m%d')}"

    with transaction.atomic():
        row, _created = (
            SequenceCounter.objects
            .select_for_update()
            .get_or_create(
                scope=scope,
                defaults={"value": _max_existing_seq(model_class, field, scope)},
            )
        )
        row.value += 1
        row.save(update_fields=["value", "updated_at"])
        return f"{scope}-{row.value:04d}"
