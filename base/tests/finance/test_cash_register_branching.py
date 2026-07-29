from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings


pytestmark = pytest.mark.django_db


@override_settings(BRANCH_ID='branch-a')
def test_register_updates_are_scoped_to_current_branch():
    from base.models import CashRegister
    from base.services.inkassa_service import InkassaService

    own = CashRegister.objects.create(branch_id='branch-a', current_balance=10)
    other = CashRegister.objects.create(branch_id='branch-b', current_balance=900)

    InkassaService.add_to_register(Decimal('5'))

    own.refresh_from_db()
    other.refresh_from_db()
    assert own.current_balance == Decimal('15')
    assert other.current_balance == Decimal('900')


def test_only_one_active_register_per_branch():
    from base.models import CashRegister

    CashRegister.objects.create(branch_id='branch-a', current_balance=10)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CashRegister.objects.create(branch_id='branch-a', current_balance=20)
    CashRegister.objects.create(
        branch_id='branch-a', current_balance=1, is_deleted=True,
    )


@override_settings(BRANCH_ID='branch-a', DEPLOYMENT_MODE='local')
def test_synced_register_reconciles_by_branch_without_overwriting_balance():
    from base.models import CashRegister

    local = CashRegister.objects.create(branch_id='branch-a', current_balance=77)
    incoming, action = CashRegister.from_sync_dict({
        'uuid': '20000000-0000-0000-0000-000000000001',
        'sync_version': local.sync_version + 1,
        'branch_id': 'branch-a',
        'current_balance': '999.00',
        'is_deleted': False,
    }, branch_id='branch-a')

    assert action == 'updated'
    assert incoming.pk == local.pk
    incoming.refresh_from_db()
    assert incoming.current_balance == Decimal('77')
