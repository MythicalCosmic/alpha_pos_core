from decimal import Decimal
from datetime import datetime, timezone, timedelta


UZB_TZ = timezone(timedelta(hours=5))


def uzb_now():
    return datetime.now(UZB_TZ)


def format_datetime(dt=None):
    if dt is None:
        dt = uzb_now()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UZB_TZ)
    else:
        dt = dt.astimezone(UZB_TZ)
    return dt.strftime('%Y-%m-%d'), dt.strftime('%H:%M:%S')


def format_money(amount):
    # Tolerant of Decimal / float / int / numeric-string / None so a template
    # render never blows up on an unexpected type.
    try:
        return f'{float(amount):,.0f}'
    except (TypeError, ValueError):
        return '0'


def format_duration_minutes(minutes):
    if minutes < 60:
        return f'{minutes} daqiqa'
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f'{hours} soat'
    return f'{hours} soat {mins} daqiqa'


def format_prep_time(seconds):
    if not seconds or seconds == 0:
        return '—'
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f'{minutes}:{secs:02d}'
