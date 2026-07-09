"""The sync receiver must turn incoming ISO date/datetime strings into real
date/datetime objects using the stdlib — NOT depend on `dateutil` and silently
fall back to the raw string when it's absent.

Regression for the "missing shift" reports: the server image has no `dateutil`,
so `_clean_field_value` swallowed the ImportError and stored the raw string.
Postgres accepts a tidy ISO string but rejects odd ones, so that row failed to
write, retried to the dead-letter cap, and went permanently missing on the cloud.
"""
import builtins
from datetime import date, datetime

from django.db import models

from base.services.sync.receiver import _clean_field_value


def test_datetime_string_becomes_datetime():
    out = _clean_field_value(models.DateTimeField(), '2026-07-09T04:00:00+00:00')
    assert isinstance(out, datetime)
    assert (out.year, out.month, out.day, out.hour) == (2026, 7, 9, 4)


def test_datetime_with_microseconds():
    out = _clean_field_value(models.DateTimeField(), '2026-07-09T04:00:00.123456+05:00')
    assert isinstance(out, datetime)
    assert out.microsecond == 123456


def test_date_string_becomes_plain_date():
    out = _clean_field_value(models.DateField(), '2026-07-09')
    assert isinstance(out, date) and not isinstance(out, datetime)
    assert out == date(2026, 7, 9)


def test_none_and_passthrough():
    assert _clean_field_value(models.DateTimeField(), None) is None
    # a value that's already a datetime is handed back untouched
    now = datetime(2026, 7, 9, 4, 0)
    assert _clean_field_value(models.DateTimeField(), now) is now


def test_parses_without_dateutil(monkeypatch):
    """The whole point: with dateutil unimportable, the stdlib path still yields
    a real datetime (not the raw string that later poisoned the DB write)."""
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == 'dateutil' or name.startswith('dateutil.'):
            raise ImportError('No module named dateutil')
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    out = _clean_field_value(models.DateTimeField(), '2026-07-09T04:00:00+00:00')
    assert isinstance(out, datetime)
    assert out.hour == 4
