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


def parse_hhmm(value):
    """"HH:MM" / "HH:MM:SS" (or a datetime.time) -> datetime.time, else None."""
    from datetime import datetime as _dt
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        return None
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return _dt.strptime(value.strip(), fmt).time()
        except (ValueError, TypeError):
            continue
    return None


def business_day_date_expr(field='created_at', start=None, tz=None):
    """A DB expression that truncates <field> to its BUSINESS date (03:00 cutover)
    for day bucketing (GROUP BY). A 01:00 sale buckets under the previous date —
    matching the business-day windowing used everywhere, instead of TruncDate at
    calendar midnight. Asia/Tashkent has no DST so the fixed-offset shift is exact."""
    from datetime import timedelta
    from django.db.models import DateTimeField, ExpressionWrapper, F
    from django.db.models.functions import TruncDate
    start = start or business_day_start()
    tz = tz or timezone.get_current_timezone()
    offset = timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
    shifted = ExpressionWrapper(F(field) - offset, output_field=DateTimeField())
    return TruncDate(shifted, tzinfo=tz)


def business_day_hour_order(start=None):
    """The 24 local hours ordered starting at the business-day cutover, e.g.
    [3,4,...,23,0,1,2] for a 03:00 start — for labelling an hourly series."""
    start = start or business_day_start()
    h0 = start.hour
    return [(h0 + i) % 24 for i in range(24)]


def tod_filter(qs, tod_from, tod_to, field='created_at', tz=None):
    """Restrict a queryset to rows whose LOCAL (tz) time-of-day is within
    [tod_from, tod_to] (datetime.time), applied PER DAY — a working-hours window
    repeated every day, NOT one continuous span. Both None -> unchanged.

    Uses TruncTime with an explicit tzinfo so the comparison is against the local
    wall-clock time (Asia/Tashkent), and .alias() (not .annotate()) so the helper
    never leaks a column into a later .values().annotate() GROUP BY. `field` may
    traverse a relation (e.g. 'order__created_at' for an OrderItem queryset)."""
    if tod_from is None and tod_to is None:
        return qs
    from django.db.models import Q
    from django.db.models.functions import TruncTime
    tz = tz or timezone.get_current_timezone()
    qs = qs.alias(_tod=TruncTime(field, tzinfo=tz))
    # A range such as 22:00 -> 02:00 crosses midnight. Treat it as the union of
    # the late-night and early-morning segments instead of applying an
    # impossible ``>= 22:00 AND <= 02:00`` predicate.
    if tod_from is not None and tod_to is not None and tod_from > tod_to:
        return qs.filter(Q(_tod__gte=tod_from) | Q(_tod__lte=tod_to))
    if tod_from is not None:
        qs = qs.filter(_tod__gte=tod_from)
    if tod_to is not None:
        qs = qs.filter(_tod__lte=tod_to)
    return qs
