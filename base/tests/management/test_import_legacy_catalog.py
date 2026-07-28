import json
from io import StringIO

import pytest
from django.core.management import call_command


def test_legacy_catalog_data_and_command_alias():
    from base.management.commands.import_legacy_catalog import (
        Command as CanonicalCommand,
        DATA_FILE,
    )
    from base.management.commands.import_old_catalog import (
        Command as LegacyCommand,
    )

    data = json.loads(DATA_FILE.read_text(encoding='utf-8'))

    assert LegacyCommand is CanonicalCommand
    assert len(data['categories']) == 15
    assert len(data['products']) == 316
    assert DATA_FILE.name == 'legacy_catalog.json'
    assert DATA_FILE.parent.name == 'data'


@pytest.mark.django_db
def test_import_legacy_catalog_dry_run_reads_moved_snapshot_without_writes():
    from base.models import Category, Product

    out = StringIO()

    call_command('import_legacy_catalog', '--dry-run', stdout=out)

    assert Category.objects.count() == 0
    assert Product.objects.count() == 0
    assert 'Loaded 15 categories, 316 products' in out.getvalue()
    assert 'rolled back, nothing written' in out.getvalue()
