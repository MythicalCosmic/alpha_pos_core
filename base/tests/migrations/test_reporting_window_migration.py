import importlib
from datetime import time

import pytest
from django.apps import apps


pytestmark = pytest.mark.django_db


def test_reporting_migration_normalizes_existing_site_settings():
    from base.models import AppSettings

    row = AppSettings.objects.create(
        business_day_start=time(3, 0),
        business_open=time(10, 0),
        business_close=time(4, 0),
    )
    migration = importlib.import_module(
        'base.migrations.0049_reporting_window_defaults'
    )

    migration.adopt_canonical_defaults(apps, None)

    row.refresh_from_db()
    assert row.business_day_start == time(7, 0)
    assert row.business_open == time(7, 0)
    assert row.business_close == time(3, 0)

