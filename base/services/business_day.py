"""Canonical restaurant reporting windows.

The cafe's operating date is deliberately shorter than a calendar day:

    D == [D 07:00, D + 1 day 03:00) in Asia/Tashkent

The quiet 03:00-07:00 interval does not belong to either operating date.  A
selected date range uses the corresponding outer bounds.  Callers that supply
an explicit ISO datetime pair instead get that exact continuous half-open
interval; this is the unambiguous contract for manual clock selections.

Legacy ``from``/``to`` and ``tod_from``/``tod_to`` inputs remain accepted by
``resolve_reporting_window``.  A legacy clock pair is interpreted as one
continuous interval (first selected date/time to final selected date/time), not
as a repeated clock predicate on every date.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime


DEFAULT_BUSINESS_DAY_START = time(7, 0)
DEFAULT_BUSINESS_DAY_END = time(3, 0)


def business_day_start():
    """Canonical opening boundary for reporting (07:00).

    This is intentionally independent of old ``business_day_start`` rows.  Old
    installations stored 03:00 as a 24-hour cutover, which cannot represent the
    required 07:00 -> 03:00 operating interval.  AppSettings still exposes the
    display defaults, while reporting has one stable contract across branches.
    """
    return DEFAULT_BUSINESS_DAY_START


def business_day_end():
    """Canonical closing boundary for reporting (03:00 next calendar day)."""
    return DEFAULT_BUSINESS_DAY_END


def business_date(moment=None, start=None):
    """Return the operating-date label for ``moment``.

    Before the 03:00 close, a moment belongs to the previous calendar date's
    service. During the 03:00-07:00 quiet gap, navigation targets the upcoming
    operating date (the current calendar date); canonical query windows still
    exclude the gap itself.
    """
    start = start or business_day_start()
    moment = moment or timezone.now()
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    d = moment.date()
    if moment.time() < business_day_end():
        d -= timedelta(days=1)
    return d


def day_window(d, start=None, end=None):
    """Aware ``[D 07:00, D+1 03:00)`` window for operating date ``d``."""
    start = start or business_day_start()
    end = end or business_day_end()
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(d, start), tz)
    hi = timezone.make_aware(datetime.combine(d + timedelta(days=1), end), tz)
    return lo, hi


def range_window(d_from, d_to, start=None, end=None):
    """Outer bounds for inclusive operating dates ``d_from..d_to``.

    The result is ``[d_from 07:00, d_to+1 03:00)``.  Use
    :func:`resolve_reporting_window` for metadata, explicit ISO datetimes and
    legacy clock inputs.
    """
    start = start or business_day_start()
    end = end or business_day_end()
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(d_from, start), tz)
    hi = timezone.make_aware(datetime.combine(d_to + timedelta(days=1), end), tz)
    return lo, hi


def today_window(start=None):
    """Current operating-date window up to now (never includes the quiet gap)."""
    now = timezone.now()
    lo, hi = day_window(business_date(now, start), start=start)
    if now < lo:
        return lo, lo
    return lo, min(now, hi)


def parse_hhmm(value):
    """``HH:MM`` / ``HH:MM:SS`` (or ``datetime.time``) -> time, else None."""
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        return None
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except (ValueError, TypeError):
            continue
    return None


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_date(str(value).strip()) if value not in (None, '') else None


def _aware_datetime(value, tz):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = parse_datetime(value.strip())
    else:
        parsed = None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, tz)
    return parsed.astimezone(tz)


@dataclass(frozen=True)
class ReportingWindow:
    """Resolved half-open interval shared by dashboard/analytics endpoints."""

    date_from: date
    date_to: date
    start_at: datetime
    end_at: datetime
    mode: str = 'business'

    @property
    def days(self):
        return (self.date_to - self.date_from).days + 1

    def metadata(self, **extra):
        data = {
            'from': self.date_from.isoformat(),
            'to': self.date_to.isoformat(),
            'start_at': self.start_at.isoformat(),
            'end_at': self.end_at.isoformat(),
            'mode': self.mode,
            'timezone': str(timezone.get_current_timezone()),
        }
        data.update(extra)
        return data

    def previous(self):
        """Immediately preceding equal-length comparison window.

        Business mode shifts by the selected number of operating dates.  Exact
        mode shifts by the exact elapsed duration.
        """
        if self.mode == 'business':
            prev_to = self.date_from - timedelta(days=1)
            prev_from = prev_to - timedelta(days=self.days - 1)
            lo, hi = range_window(prev_from, prev_to)
            return ReportingWindow(prev_from, prev_to, lo, hi, self.mode)
        duration = self.end_at - self.start_at
        lo = self.start_at - duration
        hi = self.start_at
        return ReportingWindow(lo.date(), (hi - timedelta(microseconds=1)).date(), lo, hi, self.mode)

    def bounds(self, field='created_at'):
        return {
            f'{field}__gte': self.start_at,
            f'{field}__lt': self.end_at,
        }

    def filter(self, qs, field='created_at'):
        """Apply the window and, in business mode, exclude every quiet gap.

        ``start_at``/``end_at`` remain the authoritative outer bounds.  For a
        multi-date business selection, local 03:00-07:00 rows between those
        bounds are not part of any selected operating date. Exact/custom mode
        is continuous and is never altered by this clock predicate.
        """
        qs = qs.filter(**self.bounds(field))
        if self.mode != 'business':
            return qs
        from django.db.models.functions import TruncTime
        alias = '_reporting_operating_time'
        qs = qs.alias(**{
            alias: TruncTime(field, tzinfo=timezone.get_current_timezone()),
        })
        return qs.filter(
            Q(**{f'{alias}__gte': business_day_start()})
            | Q(**{f'{alias}__lt': business_day_end()})
        )


def resolve_reporting_window(
    date_from=None,
    date_to=None,
    *,
    datetime_from=None,
    datetime_to=None,
    from_at=None,
    to_at=None,
    tod_from=None,
    tod_to=None,
    default_date=None,
):
    """Resolve canonical date or custom datetime parameters.

    Parameter precedence:

    1. ``datetime_from`` / ``datetime_to`` (aliases ``from_at`` / ``to_at``)
    2. legacy dates plus ``tod_from`` / ``tod_to`` as one continuous interval
    3. default operating dates, using 07:00 -> next-day 03:00.

    Invalid or half-specified custom pairs raise ``ValueError`` so views can
    return a useful 422 rather than silently count a different period.
    """
    tz = timezone.get_current_timezone()
    raw_start = datetime_from or from_at
    raw_end = datetime_to or to_at
    if raw_start is not None or raw_end is not None:
        if raw_start is None or raw_end is None:
            raise ValueError('datetime_from and datetime_to must be supplied together')
        lo = _aware_datetime(raw_start, tz)
        hi = _aware_datetime(raw_end, tz)
        if lo is None or hi is None:
            raise ValueError('datetime_from/datetime_to must be valid ISO datetimes')
        if hi <= lo:
            raise ValueError('datetime_to must be after datetime_from')
        return ReportingWindow(
            lo.date(), (hi - timedelta(microseconds=1)).date(), lo, hi, 'custom',
        )

    fallback = default_date or business_date()
    d_from = _date(date_from) or fallback
    d_to = _date(date_to) or d_from
    if date_from not in (None, '') and _date(date_from) is None:
        raise ValueError('from/date_from must be YYYY-MM-DD')
    if date_to not in (None, '') and _date(date_to) is None:
        raise ValueError('to/date_to must be YYYY-MM-DD')
    if d_to < d_from:
        d_from, d_to = d_to, d_from

    tf, tt = parse_hhmm(tod_from), parse_hhmm(tod_to)
    # Legacy invalid clock values were historically ignored. Keep that
    # compatibility; the canonical ISO datetime pair remains strict.
    if tf is not None or tt is not None:
        # Preserve single-sided legacy inputs by filling the missing boundary
        # with the canonical opening/closing clock.
        tf = tf or business_day_start()
        tt = tt or business_day_end()
        lo = timezone.make_aware(datetime.combine(d_from, tf), tz)
        hi = timezone.make_aware(datetime.combine(d_to, tt), tz)
        # A close clock at/before the open clock always denotes the next
        # calendar day relative to the selected final date. This holds for a
        # multi-date selection too (Jul 10..11, 22:00->02:00 ends Jul 12 02:00).
        if tt <= tf:
            hi += timedelta(days=1)
        return ReportingWindow(d_from, (hi - timedelta(microseconds=1)).date(), lo, hi, 'custom')

    lo, hi = range_window(d_from, d_to)
    return ReportingWindow(d_from, d_to, lo, hi, 'business')


def request_window_params(query):
    """Extract the canonical/legacy reporting parameters from ``request.GET``."""
    return {
        'date_from': query.get('date_from') or query.get('from'),
        'date_to': query.get('date_to') or query.get('to'),
        'datetime_from': query.get('datetime_from'),
        'datetime_to': query.get('datetime_to'),
        'from_at': query.get('from_at'),
        'to_at': query.get('to_at'),
        'tod_from': query.get('tod_from'),
        'tod_to': query.get('tod_to'),
    }


def business_day_date_expr(field='created_at', start=None, tz=None):
    """DB expression assigning events to the 07:00-labelled operating date."""
    from django.db.models import DateTimeField, ExpressionWrapper, F
    from django.db.models.functions import TruncDate
    start = start or business_day_start()
    tz = tz or timezone.get_current_timezone()
    offset = timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
    shifted = ExpressionWrapper(F(field) - offset, output_field=DateTimeField())
    return TruncDate(shifted, tzinfo=tz)


def business_day_hour_order(start=None):
    """The 20 operating hours ordered 07:00..02:00."""
    start = start or business_day_start()
    close = business_day_end()
    hours = []
    current = start.hour
    while current != close.hour:
        hours.append(current)
        current = (current + 1) % 24
    return hours


def tod_filter(qs, tod_from, tod_to, field='created_at', tz=None):
    """Legacy repeated local-time filter.

    New request handlers should resolve a :class:`ReportingWindow` instead.
    This helper remains for internal callers and older direct service tests.
    """
    if tod_from is None and tod_to is None:
        return qs
    from django.db.models.functions import TruncTime
    tz = tz or timezone.get_current_timezone()
    qs = qs.alias(_tod=TruncTime(field, tzinfo=tz))
    if tod_from is not None and tod_to is not None and tod_from > tod_to:
        return qs.filter(Q(_tod__gte=tod_from) | Q(_tod__lt=tod_to))
    if tod_from is not None:
        qs = qs.filter(_tod__gte=tod_from)
    if tod_to is not None:
        qs = qs.filter(_tod__lt=tod_to)
    return qs
