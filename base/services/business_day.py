from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime


DEFAULT_BUSINESS_DAY_START = time(7, 0)
DEFAULT_BUSINESS_DAY_END = time(3, 0)


def business_day_start():
    return DEFAULT_BUSINESS_DAY_START


def business_day_end():
    return DEFAULT_BUSINESS_DAY_END


def business_date(moment=None, start=None):
    start = start or business_day_start()
    moment = moment or timezone.now()
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    d = moment.date()
    if moment.time() < business_day_end():
        d -= timedelta(days=1)
    return d


def day_window(d, start=None, end=None):
    start = start or business_day_start()
    end = end or business_day_end()
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(d, start), tz)
    hi = timezone.make_aware(datetime.combine(d + timedelta(days=1), end), tz)
    return lo, hi


def range_window(d_from, d_to, start=None, end=None):
    start = start or business_day_start()
    end = end or business_day_end()
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(d_from, start), tz)
    hi = timezone.make_aware(datetime.combine(d_to + timedelta(days=1), end), tz)
    return lo, hi


def today_window(start=None):
    now = timezone.now()
    lo, hi = day_window(business_date(now, start), start=start)
    if now < lo:
        return lo, lo
    return lo, min(now, hi)


def parse_hhmm(value):
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
    if tf is not None or tt is not None:
        tf = tf or business_day_start()
        tt = tt or business_day_end()
        lo = timezone.make_aware(datetime.combine(d_from, tf), tz)
        hi = timezone.make_aware(datetime.combine(d_to, tt), tz)
        if tt <= tf:
            hi += timedelta(days=1)
        return ReportingWindow(d_from, (hi - timedelta(microseconds=1)).date(), lo, hi, 'custom')

    lo, hi = range_window(d_from, d_to)
    return ReportingWindow(d_from, d_to, lo, hi, 'business')


def request_window_params(query):
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
    from django.db.models import DateTimeField, ExpressionWrapper, F
    from django.db.models.functions import TruncDate
    start = start or business_day_start()
    tz = tz or timezone.get_current_timezone()
    offset = timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
    shifted = ExpressionWrapper(F(field) - offset, output_field=DateTimeField())
    return TruncDate(shifted, tzinfo=tz)


def business_day_hour_order(start=None):
    start = start or business_day_start()
    close = business_day_end()
    hours = []
    current = start.hour
    while current != close.hour:
        hours.append(current)
        current = (current + 1) % 24
    return hours


def tod_filter(qs, tod_from, tod_to, field='created_at', tz=None):
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
