import pytest
from django.db import IntegrityError, transaction


pytestmark = pytest.mark.django_db


def test_only_one_active_treasury_account_per_kind():
    from base.models import TreasuryAccount

    TreasuryAccount.objects.create(kind='SAFE', balance='10')
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TreasuryAccount.objects.create(kind='SAFE', balance='20')


def test_soft_deleted_historical_account_does_not_block_replacement():
    from base.models import TreasuryAccount

    old = TreasuryAccount.objects.create(kind='BANK', balance='10')
    old.delete()

    replacement = TreasuryAccount.objects.create(kind='BANK', balance='20')

    assert replacement.is_deleted is False


@pytest.mark.django_db(transaction=True)
def test_treasury_migration_merges_legacy_duplicate_balances():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [('base', '0040_order_paid_at_index')]
    after = [('base', '0041_unique_active_treasury_account')]

    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old_apps = executor.loader.project_state(before).apps
    OldAccount = old_apps.get_model('base', 'TreasuryAccount')
    OldAccount.objects.create(kind='SAFE', balance='10')
    OldAccount.objects.create(kind='SAFE', balance='20')

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    new_apps = executor.loader.project_state(after).apps
    Account = new_apps.get_model('base', 'TreasuryAccount')
    rows = list(Account.objects.filter(kind='SAFE').order_by('pk'))

    assert len(rows) == 2
    assert rows[0].is_deleted is False
    assert rows[0].balance == 30
    assert rows[1].is_deleted is True
