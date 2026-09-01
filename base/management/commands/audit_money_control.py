import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum
from django.utils import timezone

from base.models import AppSettings, TreasuryTransaction
from base.money import uzs_int
from base.services.business_day import business_date
from base.services.money_control_service import MoneyControlService
from cashbox.models import CashboxExpense, CashboxExpenseCategory
from hr.models import Expense
from stock.models import PurchaseOrder, SupplierPayment
from stock.services.inventory_control_service import InventoryControlService


class Command(BaseCommand):
    help = 'Produce a read-only Money Control reconciliation report.'

    def add_arguments(self, parser):
        parser.add_argument('--branch', required=True)
        parser.add_argument('--as-of', required=True)
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        try:
            as_of = datetime.fromisoformat(options['as_of'])
        except ValueError as exc:
            raise CommandError('--as-of must be an ISO-8601 datetime') from exc
        if timezone.is_naive(as_of):
            as_of = timezone.make_aware(as_of, timezone.get_current_timezone())
        branch_id = options['branch'].strip()
        day = business_date(as_of)
        inventory_result, inventory_status = InventoryControlService.get(
            branch_id=branch_id,
            item_type='RAW',
            page=1,
            per_page=1,
            as_of=as_of,
        )
        if inventory_status >= 400:
            raise CommandError(inventory_result.get('message', 'Inventory audit failed'))
        treasury, treasury_issues = MoneyControlService._treasury(branch_id)
        drawer, drawer_issues = MoneyControlService._drawer(branch_id)
        suppliers, supplier_issues = MoneyControlService._suppliers(
            branch_id,
            as_of,
        )
        expenses, expense_issues = MoneyControlService._expenses(
            branch_id,
            day,
            day,
        )
        issues = (
            inventory_result['data']['completeness']['issues']
            + treasury_issues
            + drawer_issues
            + supplier_issues
            + expense_issues
        )
        category_mapping = self._category_mapping(branch_id)
        supplier_payment_backfill = self._supplier_payments(branch_id)
        expense_deduplication = self._expense_deduplication(branch_id)
        settings = AppSettings.load()
        report = {
            'command': 'audit_money_control',
            'read_only': True,
            'branch_id': branch_id,
            'as_of': as_of.isoformat(),
            'shift_settlement_cutover_at': (
                settings.shift_settlement_cutover_at.isoformat()
            ),
            'treasury': {**treasury, 'drawer_unreconciled_uzs': drawer},
            'suppliers': suppliers,
            'inventory': inventory_result['data']['summary'],
            'expenses': expenses,
            'issue_count': len(issues),
            'issues': issues,
            'category_mapping_dry_run': category_mapping,
            'legacy_supplier_payment_dry_run': supplier_payment_backfill,
            'expense_deduplication_dry_run': expense_deduplication,
        }
        payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        destination = options['output']
        if destination == '-':
            self.stdout.write(payload)
        else:
            path = Path(destination).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload + '\n', encoding='utf-8')
            self.stdout.write(str(path))

    @staticmethod
    def _category_mapping(branch_id):
        legacy_categories = [{
            'legacy_category_id': row.id,
            'name': row.name,
            'reporting_group': row.reporting_group,
            'canonical_category_id': row.canonical_category_id,
            'status': 'MAPPED' if row.canonical_category_id else 'UNMAPPED',
        } for row in CashboxExpenseCategory.objects.filter(
            is_deleted=False,
        ).order_by('id')]
        unmapped_treasury = [{
            'treasury_transaction_id': row.id,
            'category_snapshot': row.category_name_snapshot or row.category,
            'amount_uzs': uzs_int(abs(row.delta)),
        } for row in TreasuryTransaction.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            type=TreasuryTransaction.Type.EXPENSE,
            canonical_category__isnull=True,
        ).order_by('id')]
        unmapped_cashbox = [{
            'cashbox_expense_id': row.id,
            'legacy_category_id': row.category_id,
            'amount_uzs': uzs_int(row.amount),
        } for row in CashboxExpense.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            reversal_of__isnull=True,
            canonical_category__isnull=True,
        ).order_by('id')]
        return {
            'read_only': True,
            'legacy_categories': legacy_categories,
            'unmapped_treasury_expenses': unmapped_treasury,
            'unmapped_cashbox_expenses': unmapped_cashbox,
        }

    @staticmethod
    def _supplier_payments(branch_id):
        legacy = [{
            'supplier_payment_id': row.id,
            'supplier_id': row.supplier_id,
            'principal_uzs': uzs_int(row.principal_uzs),
            'source_account': row.source_account or None,
            'status': row.status,
        } for row in SupplierPayment.objects.filter(
            branch_id=branch_id,
            status=SupplierPayment.Status.LEGACY_UNFUNDED,
        ).order_by('id')]
        allocation_totals = {
            row['allocations__purchase_order_id']: row['total'] or 0
            for row in SupplierPayment.objects.filter(
                branch_id=branch_id,
                status__in=[
                    SupplierPayment.Status.POSTED,
                    SupplierPayment.Status.LEGACY_UNFUNDED,
                ],
            ).values('allocations__purchase_order_id').annotate(
                total=Sum('allocations__amount_uzs'),
            )
            if row['allocations__purchase_order_id'] is not None
        }
        mismatches = [{
            'purchase_order_id': po.id,
            'stored_amount_paid_uzs': uzs_int(po.amount_paid),
            'funded_allocation_uzs': uzs_int(allocation_totals.get(po.id, 0)),
        } for po in PurchaseOrder.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
        ).order_by('id') if po.amount_paid != allocation_totals.get(po.id, 0)]
        return {
            'read_only': True,
            'legacy_unfunded_payments': legacy,
            'purchase_order_allocation_mismatches': mismatches,
        }

    @staticmethod
    def _expense_deduplication(branch_id):
        missing = []
        duplicates = []
        for expense in Expense.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            status__in=[Expense.Status.PAID, Expense.Status.VOIDED],
        ).select_related('cashbox_payment').order_by('id'):
            try:
                cashbox = expense.cashbox_payment
            except CashboxExpense.DoesNotExist:
                cashbox = None
            links = int(bool(expense.treasury_transaction_id)) + int(bool(cashbox))
            evidence = {
                'expense_id': expense.id,
                'treasury_transaction_id': expense.treasury_transaction_id,
                'cashbox_expense_id': cashbox.id if cashbox else None,
            }
            if links == 0:
                missing.append(evidence)
            elif links > 1:
                duplicates.append(evidence)
        standalone_treasury = list(TreasuryTransaction.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            type=TreasuryTransaction.Type.EXPENSE,
            expense_payment__isnull=True,
        ).order_by('id').values_list('id', flat=True))
        standalone_cashbox = list(CashboxExpense.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            reversal_of__isnull=True,
            canonical_expense__isnull=True,
            recipient_supplier__isnull=True,
        ).order_by('id').values_list('id', flat=True))
        return {
            'read_only': True,
            'canonical_expenses_missing_payment_link': missing,
            'canonical_expenses_with_duplicate_links': duplicates,
            'standalone_treasury_expense_ids': standalone_treasury,
            'standalone_cashbox_expense_ids': standalone_cashbox,
        }
