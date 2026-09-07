from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import transaction

from base.models import TreasuryTransaction, User
from base.security.permission_catalog import DEFAULT_ROLE_PERMISSIONS
from base.services.treasury_service import _apply, _lock_accounts
from stock.models import Supplier
from stock.tests.purchase_invoices.conftest import invoice_data  # noqa: F401
from stock.tests.test_warehouse_receiving import _client


@pytest.fixture
def opening_data():
    actor = User.objects.create(
        first_name="Opening",
        last_name="Reviewer",
        email="opening-reviewer@test.local",
        password="!",
        role="MANAGER",
        status="ACTIVE",
        branch_id="branch1",
        permissions=DEFAULT_ROLE_PERMISSIONS["MANAGER"],
    )
    supplier = Supplier.objects.create(
        name="Opening supplier", current_balance=5000000, branch_id="branch1"
    )
    with transaction.atomic():
        bank = _lock_accounts(["BANK"], "branch1")["BANK"]
        _apply(
            bank,
            Decimal("10000000"),
            TreasuryTransaction.Type.ADJUSTMENT,
            branch_id="branch1",
            performed_by=actor,
        )
    payload = {
        "amount_uzs": 5000000,
        "as_of_date": "2026-09-01",
        "mode": "RECONCILE_EXISTING",
        "reason": "Opening debt checked against supplier statement",
        "source_reference": "Statement 2026-09-01",
        "confirmed": True,
    }
    return SimpleNamespace(
        actor=actor,
        supplier=supplier,
        bank=bank,
        payload=payload,
        client=_client(actor),
    )
