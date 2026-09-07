"""Read-only rollout report; also runs before the new columns exist."""

import json

from django.core.management.base import BaseCommand

from base.models import User
from stock.models import PurchaseReceiving, PurchaseReceivingItem, SupplierStockItem
from stock.services.purchase_invoices.validation import UNKNOWN_PRICE_MARKER


class Command(BaseCommand):
    help = "Print a read-only direct invoice migration impact report (never changes accounting data)."

    def handle(self, *args, **options):
        links = SupplierStockItem.objects.all()
        report = {
            "dry_run": True,
            "receiving_rows": PurchaseReceiving.objects.count(),
            "receiving_line_rows": PurchaseReceivingItem.objects.count(),
            "supplier_links_with_exact_unknown_marker": links.filter(
                notes__contains=UNKNOWN_PRICE_MARKER
            ).count(),
            "zero_price_supplier_links": links.filter(price=0).count(),
            "positive_catalog_prices_without_unknown_marker": links.filter(price__gt=0)
            .exclude(notes__contains=UNKNOWN_PRICE_MARKER)
            .count(),
            "role_grant_candidates": {
                role: User.objects.filter(role=role, is_deleted=False).count()
                for role in ("WAREHOUSE", "MANAGER")
            },
            "accounting_records_rewritten": False,
            "backfill": [
                "existing PO supplier identity",
                "existing PO supplier-item identity",
                "explicit known/unknown price state",
                "invoice permission grants",
            ],
        }
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
