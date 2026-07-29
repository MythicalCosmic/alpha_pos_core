"""Database-serialized publication clock for the cloud change feed.

The feed cursor is a timestamp, so assigning publication times directly from
the operating-system clock is unsafe when that clock moves backwards.  This
module turns the existing ``SyncState`` table into a small logical clock.  No
schema change is required.

Callers must hold a database transaction while allocating a timestamp and must
keep the logical-clock write in the same transaction as the publication write
it protects.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime


CLOUD_FEED_CLOCK_KEY = 'cloud_change_feed_clock_v1'
_TICK = timedelta(microseconds=1)


def _latest_existing_publication(*, using):
    """Find the highest timestamp that predates logical-clock installation."""
    from base.services.sync.config import get_all_models

    latest = None
    for model in get_all_models().values():
        value = (
            model._base_manager.using(using)
            .aggregate(latest=Max('synced_at'))
            .get('latest')
        )
        if value is not None and (latest is None or value > latest):
            latest = value
    return latest


def _next_tick(value):
    try:
        return value + _TICK
    except OverflowError:
        # Saturation is fail-safe. Rows published at datetime.max replay at the
        # cursor boundary; they may duplicate, but they cannot be skipped.
        return value


def allocate_cloud_feed_timestamp(*, using='default'):
    """Advance and return the cloud feed's durable logical timestamp.

    The caller must already be inside ``transaction.atomic(using=using)``.
    ``select_for_update`` serializes snapshot cutoffs with content publishers,
    establishing this invariant:

    * a publication committed before a snapshot has a timestamp at or below
      its cutoff;
    * a publication committed after that snapshot has a strictly newer
      timestamp and is therefore visible on the next pull.

    Client cursors deliberately are not accepted as a floor. They are
    authenticated but still remote input; allowing one corrupt terminal to
    submit ``9999-12-31`` would poison this shared clock for every branch.
    """
    from base.models import SyncState

    if not transaction.get_connection(using).in_atomic_block:
        raise RuntimeError(
            'cloud feed timestamps must be allocated inside transaction.atomic',
        )

    state, created = (
        SyncState.objects.using(using)
        .select_for_update()
        .get_or_create(
            key=CLOUD_FEED_CLOCK_KEY,
            defaults={'value': ''},
        )
    )

    previous = parse_datetime(state.value) if state.value else None
    if previous is not None and timezone.is_naive(previous):
        previous = timezone.make_aware(
            previous,
            timezone.get_default_timezone(),
        )
    if created or previous is None:
        existing = _latest_existing_publication(using=using)
        if existing is not None and (previous is None or existing > previous):
            previous = existing

    now = timezone.now()
    candidates = [now]
    if previous is not None:
        candidates.append(_next_tick(previous))
    allocated = max(candidates)

    state.value = allocated.isoformat()
    state.save(update_fields=['value', 'updated_at'])
    return allocated
