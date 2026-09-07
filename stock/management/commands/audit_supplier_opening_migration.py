"""Read-only impact report; never registers or approves supplier debt."""

import json

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Exists, OuterRef

from stock.models import Supplier, SupplierPayment, SupplierTransaction


class Command(BaseCommand):
    help = "Report supplier opening-debt migration impact without changing balances."

    def handle(self, *args, **options):
        suppliers = Supplier.objects.filter(is_deleted=False).annotate(
            has_history=Exists(
                SupplierTransaction.objects.filter(supplier_id=OuterRef("pk"))
            ),
            has_payments=Exists(
                SupplierPayment.objects.filter(supplier_id=OuterRef("pk"))
            ),
        )
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        self.stdout.write(
            json.dumps(
                {
                    "dry_run": True,
                    "suppliers_requiring_opening_review": suppliers.filter(
                        current_balance__gt=0, has_history=False, has_payments=False
                    ).count(),
                    "financial_rows_to_create": 0,
                    "accounting_records_rewritten": False,
                    "existing_balances_changed": False,
                    "automatic_debt_approval": False,
                    "pending_migrations": [
                        f"{migration.app_label}.{migration.name}"
                        for migration, backwards in pending
                        if not backwards
                    ],
                },
                indent=2,
            )
        )
