from collections import defaultdict
from decimal import Decimal

from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from base.models import (
    CashReconciliation,
    PaymentMethodConfig,
    Shift,
    TreasuryAccount,
    TreasuryTransaction,
)
from base.money import uzs_int
from base.services.branch_scope import resolve_actor_branch
from base.services.business_day import range_window
from cashbox.models import CashboxExpense
from hr.models import Expense
from stock.models import (
    PurchaseOrder,
    PurchaseReceiving,
    Supplier,
    SupplierPayment,
    SupplierTransaction,
)
from stock.services.inventory_control_service import InventoryControlService, issue


WORKING_CAPITAL_FORMULA = (
    'SAFE + BANK + DRAWER_UNRECONCILED + RAW_INVENTORY '
    '- SUPPLIER_PAYABLE + SUPPLIER_CREDIT'
)


def _is_whole(value):
    value = Decimal(value or 0)
    return value == value.to_integral_value()


def _unique_issues(rows):
    seen = set()
    output = []
    for row in rows:
        key = (
            row['code'],
            row.get('entity_type'),
            row.get('entity_id'),
            repr(sorted((row.get('details') or {}).items())),
        )
        if key not in seen:
            seen.add(key)
            output.append(row)
    return output


class MoneyControlService:
    @classmethod
    def overview(cls, *, actor, date_from, date_to, location_id=None):
        branch_id = str(resolve_actor_branch(actor) or '').strip()
        if not branch_id:
            from base.helpers.response import ServiceResponse

            return ServiceResponse.failure(
                'BRANCH_SCOPE_REQUIRED',
                'Money-control branch could not be resolved.',
                403,
            )
        starts_snapshot = not connection.in_atomic_block
        with transaction.atomic():
            if connection.vendor == 'postgresql' and starts_snapshot:
                with connection.cursor() as cursor:
                    cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            as_of = timezone.now()
            inventory_result, inventory_status = InventoryControlService.get(
                actor=actor,
                branch_id=branch_id,
                item_type='RAW',
                location_id=location_id,
                page=1,
                per_page=1,
                as_of=as_of,
            )
            if inventory_status >= 400:
                return inventory_result, inventory_status
            inventory_data = inventory_result['data']
            treasury, treasury_issues = cls._treasury(branch_id)
            drawer, drawer_issues = cls._drawer(
                branch_id,
                location_id=location_id,
            )
            suppliers, supplier_issues = cls._suppliers(branch_id, as_of)
            expenses, expense_issues = cls._expenses(
                branch_id,
                date_from,
                date_to,
            )
            issues = _unique_issues(
                inventory_data['completeness']['issues']
                + treasury_issues
                + drawer_issues
                + supplier_issues
                + expense_issues
            )

            safe = treasury['safe_uzs']
            bank = treasury['bank_uzs']
            liquid_parts = [drawer, safe, bank]
            liquid = sum(liquid_parts) if all(
                value is not None for value in liquid_parts
            ) else None
            treasury['drawer_unreconciled_uzs'] = drawer
            treasury['liquid_total_uzs'] = liquid
            inventory_summary = inventory_data['summary']
            inventory = {
                'raw_material_value_uzs': inventory_summary['inventory_value_uzs'],
                'raw_available_value_uzs': inventory_summary['available_value_uzs'],
                'raw_item_count': inventory_summary['raw_item_count'],
                'low_stock_count': inventory_summary['low_stock_count'],
                'out_of_stock_count': inventory_summary['out_of_stock_count'],
                'valuation_method': 'WEIGHTED_AVERAGE',
            }
            capital_parts = [
                safe,
                bank,
                drawer,
                inventory['raw_material_value_uzs'],
                suppliers['payable_uzs'],
                suppliers['credit_uzs'],
            ]
            if all(value is not None for value in capital_parts):
                working_capital = (
                    safe + bank + drawer
                    + inventory['raw_material_value_uzs']
                    - suppliers['payable_uzs']
                    + suppliers['credit_uzs']
                )
            else:
                working_capital = None
                issues.append(issue(
                    'WORKING_CAPITAL_COMPONENT_INCOMPLETE',
                    'ERROR',
                    'Working-capital snapshot is incomplete',
                    'At least one formula component is not trustworthy.',
                    details={
                        'missing_components': [
                            name for name, value in zip(
                                [
                                    'SAFE', 'BANK', 'DRAWER_UNRECONCILED',
                                    'RAW_INVENTORY', 'SUPPLIER_PAYABLE',
                                    'SUPPLIER_CREDIT',
                                ],
                                capital_parts,
                            ) if value is None
                        ],
                    },
                ))
            issues = _unique_issues(issues)
            has_error = any(row['severity'] == 'ERROR' for row in issues)
            completeness = (
                'UNSAFE' if has_error else ('PARTIAL' if issues else 'COMPLETE')
            )
            reconciliation = (
                'INCOMPLETE' if working_capital is None
                else ('WARNING' if issues else 'BALANCED')
            )
            from base.helpers.response import ServiceResponse

            return ServiceResponse.success(data={
                'as_of': timezone.localtime(as_of).isoformat(),
                'period': {
                    'date_from': date_from.isoformat(),
                    'date_to': date_to.isoformat(),
                    'timezone': 'Asia/Tashkent',
                },
                'scope': {
                    'branch_id': branch_id,
                    'location_id': location_id,
                    'treasury': 'BRANCH',
                    'inventory': 'LOCATION' if location_id else 'BRANCH',
                },
                'completeness': {
                    'status': completeness,
                    'issues': issues,
                },
                'treasury': treasury,
                'suppliers': suppliers,
                'inventory': inventory,
                'expenses': expenses,
                'working_capital': {
                    'amount_uzs': working_capital,
                    'formula': WORKING_CAPITAL_FORMULA,
                },
                'reconciliation': {
                    'status': reconciliation,
                    'issues': issues,
                },
            })

    @staticmethod
    def _treasury(branch_id):
        accounts = {
            row.kind: row
            for row in TreasuryAccount.objects.filter(
                branch_id=branch_id,
                is_deleted=False,
            ).order_by('id')
        }
        result = {'safe_uzs': None, 'bank_uzs': None}
        issues = []
        for kind, key in [
            (TreasuryAccount.Kind.SAFE, 'safe_uzs'),
            (TreasuryAccount.Kind.BANK, 'bank_uzs'),
        ]:
            account = accounts.get(kind)
            if account is None:
                issues.append(issue(
                    'TREASURY_ACCOUNT_MISSING',
                    'ERROR',
                    'Treasury account is missing',
                    f'The branch has no active {kind} account.',
                    entity_type='TreasuryAccount',
                    details={'account': kind, 'branch_id': branch_id},
                ))
                continue
            rows = TreasuryTransaction.objects.filter(
                account=account,
                branch_id=branch_id,
                is_deleted=False,
            ).order_by('created_at', 'id').values(
                'balance_before', 'balance_after', 'delta', 'fee',
            )
            valid = _is_whole(account.balance)
            running = Decimal('0')
            first = True
            for row in rows:
                if first and row['balance_before'] != 0:
                    valid = False
                first = False
                if (
                    row['balance_before'] != running
                    or row['balance_after']
                    != row['balance_before'] + row['delta']
                ):
                    valid = False
                running = row['balance_after']
                if not all(_is_whole(value) for value in (
                    row['delta'],
                    row['fee'],
                    row['balance_before'],
                    row['balance_after'],
                )):
                    valid = False
            if running != (account.balance or Decimal('0')):
                valid = False
            if not valid:
                issues.append(issue(
                    'TREASURY_LEDGER_BALANCE_MISMATCH',
                    'ERROR',
                    'Treasury balance disagrees with its ledger',
                    f'{kind} cannot be trusted until its ledger is reconciled.',
                    entity_type='TreasuryAccount',
                    entity_id=account.id,
                    details={
                        'account': kind,
                        'stored_balance': str(account.balance),
                        'ledger_balance': str(running),
                    },
                ))
            else:
                result[key] = uzs_int(account.balance)

        fixed_destinations = {
            'CASH': TreasuryAccount.Kind.SAFE,
            'CARD': TreasuryAccount.Kind.BANK,
            'UZCARD': TreasuryAccount.Kind.BANK,
            'HUMO': TreasuryAccount.Kind.BANK,
            'PAYME': TreasuryAccount.Kind.BANK,
        }
        configured_destinations = {
            str(code).upper(): destination
            for code, destination in PaymentMethodConfig.objects.filter(
                is_active=True,
                treasury_destination=PaymentMethodConfig.TreasuryDestination.BANK,
            ).values_list('code', 'treasury_destination')
        }
        destinations = {**configured_destinations, **fixed_destinations}
        settlement_rows = TreasuryTransaction.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
            reference_type='ShiftSettlement',
        ).select_related('account').order_by('id')
        reclassified_ids = set(TreasuryTransaction.objects.filter(
            branch_id=branch_id,
            reference_type='LegacyShiftReclassification',
            type=TreasuryTransaction.Type.SHIFT_RECLASS_OUT,
        ).values_list('reference_id', flat=True))
        for row in settlement_rows:
            if row.id in reclassified_ids:
                continue
            method = str(row.category or '').upper()
            expected_destination = destinations.get(method)
            if expected_destination == row.account.kind:
                continue
            issues.append(issue(
                'LEGACY_SHIFT_TENDER_DESTINATION_AMBIGUOUS',
                'WARNING',
                'Legacy shift tender destination is ambiguous',
                'The append-only deposit remains unchanged pending review.',
                entity_type='TreasuryTransaction',
                entity_id=row.id,
                amount_uzs=uzs_int(row.delta),
                details={
                    'shift_id': row.reference_id,
                    'method': method or None,
                    'account': row.account.kind,
                    'expected_destination': expected_destination,
                },
            ))
        return result, issues

    @staticmethod
    def _drawer(branch_id, *, location_id=None):
        if location_id is not None:
            return None, [issue(
                'UNSETTLED_SHIFT_DATA_INCOMPLETE',
                'ERROR',
                'Drawer cash is not location-attributed',
                'Existing shifts are branch-owned and cannot be safely filtered by stock location.',
                details={'location_id': location_id},
            )]
        reconciled_shift_ids = CashReconciliation.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            treasury_posted_at__isnull=False,
        ).values_list('shift_id', flat=True)
        shifts = list(Shift.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            status__in=[Shift.Status.ACTIVE, Shift.Status.ENDED],
        ).exclude(pk__in=reconciled_shift_ids).select_related('user').prefetch_related(
            'payment_totals',
        ).order_by('id'))
        total = Decimal('0')
        issues = []
        ambiguous_completed = CashReconciliation.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            shift__status=Shift.Status.COMPLETED,
            treasury_posted_at__isnull=True,
        ).values_list('shift_id', flat=True)
        for shift_id in ambiguous_completed:
            issues.append(issue(
                'UNSETTLED_SHIFT_DATA_INCOMPLETE',
                'ERROR',
                'Legacy completed shift has no Treasury posting link',
                'Drawer cash cannot be classified without risking double counting.',
                entity_type='Shift',
                entity_id=shift_id,
                details={'status': Shift.Status.COMPLETED},
            ))
        for shift in shifts:
            try:
                if (
                    shift.status == Shift.Status.ENDED
                    and not shift.treasury_settlement_eligible
                ):
                    raise ValueError('legacy shift settlement is ambiguous')
                if shift.status == Shift.Status.ACTIVE:
                    from cashbox.services.drawer import drawer_cash

                    amount = drawer_cash(shift)
                else:
                    row = next((
                        value for value in shift.payment_totals.all()
                        if value.method == 'CASH' and not value.is_deleted
                    ), None)
                    if row is None:
                        raise ValueError('missing CASH close total')
                    amount = row.expected_amount
                if amount is None or amount < 0 or not _is_whole(amount):
                    raise ValueError('invalid CASH total')
                total += amount
            except Exception:
                issues.append(issue(
                    'UNSETTLED_SHIFT_DATA_INCOMPLETE',
                    'ERROR',
                    'Unsettled shift cash is incomplete',
                    'A shift lacks trustworthy physical CASH evidence.',
                    entity_type='Shift',
                    entity_id=shift.id,
                    details={'status': shift.status},
                ))
        return (None if issues else uzs_int(total)), issues

    @classmethod
    def _suppliers(cls, branch_id, as_of):
        suppliers = list(Supplier.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
        ).order_by('id'))
        ledger_rows = defaultdict(list)
        for row in SupplierTransaction.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
        ).select_related('supplier').order_by('supplier_id', 'created_at', 'id'):
            ledger_rows[row.supplier_id].append(row)
        payable = Decimal('0')
        credit = Decimal('0')
        balances_safe = True
        issues = []
        top = []
        for supplier in suppliers:
            balance = supplier.current_balance or Decimal('0')
            if supplier.currency != 'UZS':
                balances_safe = False
                issues.append(issue(
                    'SUPPLIER_CURRENCY_UNSUPPORTED',
                    'ERROR',
                    'Supplier currency is not supported',
                    'Foreign-currency balance is excluded from UZS totals.',
                    entity_type='Supplier',
                    entity_id=supplier.id,
                    details={'currency': supplier.currency},
                ))
                top.append({
                    'supplier_id': supplier.id,
                    'supplier_name': supplier.name,
                    'balance_uzs': None,
                    'payable_uzs': None,
                    'credit_uzs': None,
                    'overdue_payable_uzs': None,
                    'currency': supplier.currency,
                })
                continue
            running = Decimal('0')
            valid = _is_whole(balance)
            rows = ledger_rows.get(supplier.id, [])
            if rows and rows[0].balance_before != 0:
                valid = False
            for row in rows:
                expected = (
                    row.balance_before - row.amount
                    if row.type in {
                        SupplierTransaction.Type.PAYMENT,
                        SupplierTransaction.Type.RETURN,
                    }
                    else row.balance_before + row.amount
                )
                if row.balance_before != running or row.balance_after != expected:
                    valid = False
                running = row.balance_after
                if not all(_is_whole(value) for value in (
                    row.amount,
                    row.fee,
                    row.balance_before,
                    row.balance_after,
                )):
                    valid = False
            if running != balance:
                valid = False
            if not valid:
                balances_safe = False
                issues.append(issue(
                    'SUPPLIER_LEDGER_BALANCE_MISMATCH',
                    'ERROR',
                    'Supplier balance disagrees with its ledger',
                    'The supplier balance needs reconciliation.',
                    entity_type='Supplier',
                    entity_id=supplier.id,
                    details={
                        'stored_balance': str(balance),
                        'ledger_balance': str(running),
                    },
                ))
            supplier_payable = max(balance, Decimal('0'))
            supplier_credit = max(-balance, Decimal('0'))
            payable += supplier_payable
            credit += supplier_credit
            top.append({
                'supplier_id': supplier.id,
                'supplier_name': supplier.name,
                'balance_uzs': uzs_int(balance),
                'payable_uzs': uzs_int(supplier_payable),
                'credit_uzs': uzs_int(supplier_credit),
                'overdue_payable_uzs': None,
                'currency': 'UZS',
            })

        overdue, overdue_by_supplier, overdue_issues = cls._overdue(
            branch_id,
            suppliers,
            as_of,
        )
        issues.extend(overdue_issues)
        for row in top:
            if row['currency'] == 'UZS':
                row['overdue_payable_uzs'] = overdue_by_supplier.get(
                    row['supplier_id']
                ) if overdue is not None else None
        top = [row for row in top if row['balance_uzs'] not in (0, None)] + [
            row for row in top if row['balance_uzs'] is None
        ]
        top.sort(key=lambda row: (
            -(abs(row['balance_uzs']) if row['balance_uzs'] is not None else -1),
            row['supplier_id'],
        ))
        legacy = SupplierPayment.objects.filter(
            branch_id=branch_id,
            status=SupplierPayment.Status.LEGACY_UNFUNDED,
        )
        for payment in legacy:
            issues.append(issue(
                'PURCHASE_PAYMENT_WITHOUT_FUNDING_SOURCE',
                'ERROR',
                'Legacy supplier payment has no funding source',
                'No SAFE or BANK debit can be proven for this payment.',
                entity_type='SupplierPayment',
                entity_id=payment.id,
                amount_uzs=uzs_int(payment.principal_uzs),
            ))
        if legacy.exists():
            balances_safe = False
        return {
            'payable_uzs': uzs_int(payable) if balances_safe else None,
            'credit_uzs': uzs_int(credit) if balances_safe else None,
            'overdue_payable_uzs': overdue,
            'count_with_balance': sum(
                1 for supplier in suppliers
                if (supplier.current_balance or Decimal('0')) != 0
            ),
            'top_balances': top[:10],
        }, issues

    @staticmethod
    def _overdue(branch_id, suppliers, as_of):
        uzs_suppliers = {
            supplier.id: supplier
            for supplier in suppliers if supplier.currency == 'UZS'
        }
        if not uzs_suppliers:
            return 0, {}, []
        receivings = {
            row.id: row.purchase_order_id
            for row in PurchaseReceiving.objects.filter(
                branch_id=branch_id,
                status=PurchaseReceiving.Status.COMPLETED,
                is_deleted=False,
            )
        }
        received_by_po = defaultdict(Decimal)
        for row in SupplierTransaction.objects.filter(
            branch_id=branch_id,
            type=SupplierTransaction.Type.PURCHASE,
            reference_type='PurchaseReceiving',
            reference_id__in=receivings,
            is_deleted=False,
        ):
            received_by_po[receivings[row.reference_id]] += row.amount
        allocation_by_po = {
            row['purchase_order_id']: row['total'] or Decimal('0')
            for row in SupplierPayment.objects.filter(
                branch_id=branch_id,
                status__in=[
                    SupplierPayment.Status.POSTED,
                    SupplierPayment.Status.LEGACY_UNFUNDED,
                ],
            ).values('allocations__purchase_order_id').annotate(
                total=Sum('allocations__amount_uzs'),
            ) if row['allocations__purchase_order_id'] is not None
        }
        orders = list(PurchaseOrder.objects.filter(
            branch_id=branch_id,
            supplier_id__in=uzs_suppliers,
            is_deleted=False,
        ).exclude(status=PurchaseOrder.Status.CANCELED).order_by('id'))
        issues = []
        overdue_by_supplier = defaultdict(Decimal)
        unexplained = {
            supplier.id: max(supplier.current_balance, Decimal('0'))
            for supplier in uzs_suppliers.values()
        }
        complete = True
        for po in orders:
            allocated = allocation_by_po.get(po.id, Decimal('0'))
            if allocated != po.amount_paid:
                complete = False
                issues.append(issue(
                    'PURCHASE_PAYMENT_WITHOUT_FUNDING_SOURCE',
                    'ERROR',
                    'Purchase-order payment lacks funded allocation evidence',
                    'The PO amount paid differs from durable payment allocations.',
                    entity_type='PurchaseOrder',
                    entity_id=po.id,
                    details={
                        'amount_paid': str(po.amount_paid),
                        'allocated': str(allocated),
                    },
                ))
            remaining = max(
                received_by_po.get(po.id, Decimal('0')) - allocated,
                Decimal('0'),
            )
            remaining = min(remaining, unexplained[po.supplier_id])
            unexplained[po.supplier_id] -= remaining
            if remaining <= 0:
                continue
            if po.payment_due_date is None:
                complete = False
                issues.append(issue(
                    'SUPPLIER_OVERDUE_DATA_INCOMPLETE',
                    'ERROR',
                    'Supplier due date is missing',
                    'Overdue payable cannot be calculated for this purchase order.',
                    entity_type='PurchaseOrder',
                    entity_id=po.id,
                ))
            elif po.payment_due_date < as_of:
                overdue_by_supplier[po.supplier_id] += remaining
        for supplier_id, remaining in unexplained.items():
            if remaining > 0:
                complete = False
                issues.append(issue(
                    'SUPPLIER_OVERDUE_DATA_INCOMPLETE',
                    'ERROR',
                    'Supplier payable lacks purchase-order due evidence',
                    'Part of the authoritative balance cannot be aged.',
                    entity_type='Supplier',
                    entity_id=supplier_id,
                    amount_uzs=uzs_int(remaining),
                ))
        if not complete:
            return None, {}, issues
        serialized = {
            supplier_id: uzs_int(value)
            for supplier_id, value in overdue_by_supplier.items()
        }
        return uzs_int(sum(overdue_by_supplier.values(), Decimal('0'))), serialized, issues

    @staticmethod
    def _expenses(branch_id, date_from, date_to):
        start_at, end_at = range_window(date_from, date_to)
        period_rows = Expense.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
        ).filter(
            Q(
                status__in=[Expense.Status.PAID, Expense.Status.VOIDED],
                paid_at__gte=start_at,
                paid_at__lt=end_at,
            )
            | Q(
                status__in=[Expense.Status.PENDING, Expense.Status.APPROVED],
                expense_date__gte=date_from,
                expense_date__lte=date_to,
            )
        ).order_by('id').values(
            'id', 'status', 'amount', 'fee_uzs', 'category_id',
            'category_name_snapshot', 'category__name',
            'treasury_transaction_id', 'treasury_reversal_id',
            'cashbox_payment__id', 'cashbox_payment__reversal__id',
            'cashbox_payment__reversal__is_deleted',
        )
        paid = Decimal('0')
        pending = Decimal('0')
        approved = Decimal('0')
        paid_safe = True
        category_rows = defaultdict(lambda: {
            'name': None,
            'amount': Decimal('0'),
            'count': 0,
        })
        issues = []
        for expense in period_rows:
            amount = expense['amount'] or Decimal('0')
            fee = expense['fee_uzs'] or Decimal('0')
            if not _is_whole(amount) or not _is_whole(fee):
                paid_safe = False
                issues.append(issue(
                    'EXPENSE_AMOUNT_INVALID',
                    'ERROR',
                    'Expense contains fractional UZS',
                    'The expense cannot be included in whole-UZS reporting.',
                    entity_type='Expense',
                    entity_id=expense['id'],
                ))
                continue
            if expense['status'] == Expense.Status.PENDING:
                pending += amount
                continue
            if expense['status'] == Expense.Status.APPROVED:
                approved += amount
                continue
            cashbox_id = expense['cashbox_payment__id']
            if expense['status'] == Expense.Status.VOIDED:
                cashbox_reversed = bool(
                    expense['cashbox_payment__reversal__id']
                    and not expense['cashbox_payment__reversal__is_deleted']
                )
                missing_reversal = (
                    expense['treasury_transaction_id']
                    and not expense['treasury_reversal_id']
                ) or (cashbox_id and not cashbox_reversed) or (
                    not expense['treasury_transaction_id'] and not cashbox_id
                )
                if missing_reversal:
                    issues.append(issue(
                        'EXPENSE_PAYMENT_LINK_MISSING',
                        'ERROR',
                        'Voided expense lacks its money reversal',
                        'The original expense remains preserved but the reversal link is missing.',
                        entity_type='Expense',
                        entity_id=expense['id'],
                    ))
                continue
            links = int(bool(expense['treasury_transaction_id'])) + int(
                bool(cashbox_id)
            )
            if links == 0:
                paid_safe = False
                issues.append(issue(
                    'EXPENSE_PAYMENT_LINK_MISSING',
                    'ERROR',
                    'Paid expense has no money posting',
                    'The expense is excluded until its payment source is proven.',
                    entity_type='Expense',
                    entity_id=expense['id'],
                    amount_uzs=uzs_int(amount + fee),
                ))
                continue
            if links > 1:
                paid_safe = False
                issues.append(issue(
                    'DUPLICATE_EXPENSE_REPORTING_SOURCE',
                    'ERROR',
                    'Expense has more than one payment source',
                    'The expense is excluded to prevent double reporting.',
                    entity_type='Expense',
                    entity_id=expense['id'],
                ))
                continue
            if expense['category_id'] is None:
                issues.append(issue(
                    'EXPENSE_CATEGORY_UNMAPPED',
                    'WARNING',
                    'Expense category is not mapped',
                    'The proven payment is included under its historical snapshot.',
                    entity_type='Expense',
                    entity_id=expense['id'],
                ))
            total = amount + fee
            paid += total
            category_key = expense['category_id']
            category_rows[category_key]['name'] = (
                expense['category_name_snapshot']
                or expense['category__name']
                or 'Unmapped'
            )
            category_rows[category_key]['amount'] += total
            category_rows[category_key]['count'] += 1

        standalone_treasury = TreasuryTransaction.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            type=TreasuryTransaction.Type.EXPENSE,
            created_at__gte=start_at,
            created_at__lt=end_at,
            expense_payment__isnull=True,
        )
        standalone_cashbox = CashboxExpense.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            created_at__gte=start_at,
            created_at__lt=end_at,
            canonical_expense__isnull=True,
            reversal_of__isnull=True,
            recipient_supplier__isnull=True,
        )
        for model_name, rows in [
            ('TreasuryTransaction', standalone_treasury),
            ('CashboxExpense', standalone_cashbox),
        ]:
            for row in rows:
                paid_safe = False
                issues.append(issue(
                    'EXPENSE_PAYMENT_LINK_MISSING',
                    'ERROR',
                    'Money posting lacks a canonical expense',
                    'The posting is excluded from expense totals until linked.',
                    entity_type=model_name,
                    entity_id=row.id,
                ))
        by_category = [{
            'category_id': category_id,
            'category_name': values['name'] or 'Unmapped',
            'paid_uzs': uzs_int(values['amount']),
            'transaction_count': values['count'],
        } for category_id, values in category_rows.items()]
        by_category.sort(key=lambda row: (
            -row['paid_uzs'],
            row['category_id'] is None,
            row['category_id'] or 0,
        ))
        return {
            'paid_uzs': uzs_int(paid) if paid_safe else None,
            'pending_uzs': uzs_int(pending),
            'approved_unpaid_uzs': uzs_int(approved),
            'fee_policy': 'INCLUDED_IN_EXPENSE_CATEGORY',
            'by_category': by_category,
        }, issues
