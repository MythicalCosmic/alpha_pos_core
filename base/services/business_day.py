"""Business-day (operating-day) windowing.

A restaurant's "day" does not end at midnight — a sale rung up at 01:00 belongs
to the night before. Every date-filtered report (dashboard, stats, shifts) should
therefore bound on the configured cutover time (``AppSettings.business_day_start``,
default 03:00) rather than on the calendar day:

    business day D  ==  [ D @ start ,  (D+1) @ start )

so the window for a single business date is a 24h span starting at the cutover.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

DEFAULT_BUSINESS_DAY_START = time(3, 0)


def business_day_start():
    """The configured per-restaurant cutover time (falls back to 03:00).

    Fail-open: if AppSettings can't be read (e.g. the column isn't migrated yet)
    the default is returned so callers never crash on a reporting query."""
    try:
        from base.models import AppSettings
        return AppSettings.load().business_day_start or DEFAULT_BUSINESS_DAY_START
    except Exception:
        return DEFAULT_BUSINESS_DAY_START


def business_date(moment=None, start=None):
    """The business date a moment belongs to. Before the cutover it still counts
    as the previous calendar day (00:30 on the 5th -> business date the 4th)."""
    start = start or business_day_start()
    moment = moment or timezone.now()
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    d = moment.date()
    if moment.time() < start:
        d = d - timedelta(days=1)
    return d


def day_window(d, start=None):
    """Aware [d @ start, (d+1) @ start) window for one business date `d`."""
    start = start or business_day_start()
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(d, start), tz)
    hi = timezone.make_aware(datetime.combine(d + timedelta(days=1), start), tz)
    return lo, hi


def range_window(d_from, d_to, start=None):
    """Aware [d_from @ start, (d_to+1) @ start) window spanning business dates
    (inclusive of both ends; swapped if reversed)."""
    start = start or business_day_start()
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(d_from, start), tz)
    hi = timezone.make_aware(datetime.combine(d_to + timedelta(days=1), start), tz)
    return lo, hi


def today_window(start=None):
    """[start of the current business day, now] — "today so far" on the
    operating calendar."""
    start = start or business_day_start()
    now = timezone.now()
    lo, _ = day_window(business_date(now, start), start)
    return lo, now
