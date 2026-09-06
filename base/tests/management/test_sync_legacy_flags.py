from io import StringIO

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('flag', ['--on-save', '--off-save'])
def test_legacy_sync_flags_explain_behavior_and_cannot_disable_durable_writes(flag, settings):
    from base.models import Category, SyncQueueRecord
    from base.services.sync.config import SyncConfig

    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'audit-branch'
    settings.SYNC_ON_SAVE = False
    SyncConfig.enable()
    out = StringIO()
    call_command('sync', flag, stdout=out)
    assert 'deprecated and has no effect' in out.getvalue()
    item = Category.objects.create(name='Legacy flag check', slug='legacy-flag-check')
    assert SyncQueueRecord.objects.filter(model_name='category', record_uuid=item.uuid).exists()
    out = StringIO()
    call_command('sync', '--config', stdout=out)
    assert 'durable queueing whenever local sync is enabled' in out.getvalue()
