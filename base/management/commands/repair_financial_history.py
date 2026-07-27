"""Repair only a pre-reviewed Alpha POS historical-money repair plan.

This is deliberately not an anomaly scanner and never guesses whether a cash
expense was already applied to the branch cursor.  A forensic review must first
produce a JSON plan containing the exact historical expense UUIDs and exact
before/after values for each legacy CASH settlement row.

Execution is split by authority:

* local: repairs the till-owned CashRegister cursor and the legacy settlement
  rows;
* cloud: repairs only the cloud copies of the legacy settlement rows.  The
  cloud must never independently move a till-owned CashRegister cursor.

The command is a dry-run unless ``--apply`` is supplied.  Apply additionally
requires the repair id and the evidence fingerprint printed by a fresh dry-run.
Every write is row-locked, atomic, and followed by append-only AuditLog evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils.dateparse import parse_datetime


_MONEY_QUANTUM = Decimal('0.01')
_MAX_MONEY = Decimal('9999999999.99')
_ACTION = 'FINANCIAL_REPAIR'


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    )


def _digest(value):
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _require_object(value, label):
    if not isinstance(value, dict):
        raise CommandError(f'{label} must be a JSON object')
    return value


def _require_exact_keys(value, keys, label):
    value = _require_object(value, label)
    actual = set(value)
    required = set(keys)
    if actual != required:
        missing = ', '.join(sorted(required - actual)) or 'none'
        unknown = ', '.join(sorted(actual - required)) or 'none'
        raise CommandError(
            f'{label} has invalid keys (missing: {missing}; unknown: {unknown})'
        )


def _uuid(value, label):
    if not isinstance(value, str):
        raise CommandError(f'{label} must be a UUID string')
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise CommandError(f'{label} is not a valid UUID') from None


def _money(value, label, *, positive=False):
    # Strings avoid the irreversible precision loss of JSON floating-point
    # numbers in a plan that may authorize a production financial write.
    if not isinstance(value, str):
        raise CommandError(f'{label} must be a decimal string')
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise CommandError(f'{label} is not a decimal number') from None
    if (
        not amount.is_finite()
        or abs(amount) > _MAX_MONEY
        or amount != amount.quantize(_MONEY_QUANTUM)
    ):
        raise CommandError(f'{label} must be finite money with at most 2 decimals')
    if positive and amount <= 0:
        raise CommandError(f'{label} must be greater than zero')
    return amount.quantize(_MONEY_QUANTUM)


def _money_text(value):
    return f'{Decimal(value).quantize(_MONEY_QUANTUM):.2f}'


def _datetime_text(value):
    return (
        value.astimezone(datetime_timezone.utc)
        .isoformat(timespec='microseconds')
        .replace('+00:00', 'Z')
    )


def _timestamp(value, label):
    if not isinstance(value, str):
        raise CommandError(f'{label} must be an ISO-8601 timestamp string')
    parsed = parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise CommandError(
            f'{label} must be a timezone-aware ISO-8601 timestamp'
        )
    return _datetime_text(parsed)


def _load_plan(path):
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except OSError as exc:
        raise CommandError(f'Unable to read repair plan: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise CommandError(f'Repair plan is not valid JSON: {exc}') from exc

    _require_exact_keys(
        raw,
        {
            'version', 'repair_id', 'operator', 'branch_id',
            'register_repair', 'shift_payment_total_repairs',
        },
        'plan',
    )
    if raw['version'] != 2:
        raise CommandError('plan.version must be 2')

    repair_id = raw['repair_id']
    if (
        not isinstance(repair_id, str)
        or not repair_id.strip()
        or len(repair_id) > 128
    ):
        raise CommandError('plan.repair_id must be a non-empty string <= 128 chars')
    repair_id = repair_id.strip()

    operator = raw['operator']
    if (
        not isinstance(operator, str)
        or not operator.strip()
        or len(operator.strip()) > 128
    ):
        raise CommandError('plan.operator must be a non-empty string <= 128 chars')
    operator = operator.strip()

    branch = raw['branch_id']
    if (
        not isinstance(branch, str)
        or not branch.strip()
        or len(branch.strip()) > 50
        or branch.strip().lower() == 'cloud'
    ):
        raise CommandError(
            'plan.branch_id must identify one non-cloud branch'
        )
    branch = branch.strip()

    register = raw['register_repair']
    _require_exact_keys(
        register,
        {
            'register_uuid', 'legacy_created_before', 'expenses',
            'expected_expense_total',
        },
        'plan.register_repair',
    )
    register_uuid = _uuid(
        register['register_uuid'], 'plan.register_repair.register_uuid',
    )
    legacy_created_before = _timestamp(
        register['legacy_created_before'],
        'plan.register_repair.legacy_created_before',
    )
    expenses = register['expenses']
    if not isinstance(expenses, list) or not expenses:
        raise CommandError('plan.register_repair.expenses must be a non-empty list')
    normalized_expenses = []
    seen_expenses = set()
    expense_sum = Decimal('0.00')
    for index, expense in enumerate(expenses):
        label = f'plan.register_repair.expenses[{index}]'
        _require_exact_keys(
            expense, {'uuid', 'amount', 'shift_uuid', 'created_at'}, label,
        )
        expense_uuid = _uuid(expense['uuid'], f'{label}.uuid')
        if expense_uuid in seen_expenses:
            raise CommandError(f'{label}.uuid is duplicated')
        seen_expenses.add(expense_uuid)
        amount = _money(expense['amount'], f'{label}.amount', positive=True)
        shift_uuid = _uuid(expense['shift_uuid'], f'{label}.shift_uuid')
        created_at = _timestamp(expense['created_at'], f'{label}.created_at')
        expense_sum += amount
        normalized_expenses.append({
            'uuid': expense_uuid,
            'amount': _money_text(amount),
            'shift_uuid': shift_uuid,
            'created_at': created_at,
        })
    expected_total = _money(
        register['expected_expense_total'],
        'plan.register_repair.expected_expense_total',
        positive=True,
    )
    if expense_sum != expected_total:
        raise CommandError(
            'plan.register_repair expense sum does not equal '
            'expected_expense_total'
        )

    shift_repairs = raw['shift_payment_total_repairs']
    if not isinstance(shift_repairs, list) or not shift_repairs:
        raise CommandError(
            'plan.shift_payment_total_repairs must be a non-empty list'
        )
    normalized_shifts = []
    seen_shifts = set()
    seen_totals = set()
    for index, repair in enumerate(shift_repairs):
        label = f'plan.shift_payment_total_repairs[{index}]'
        _require_exact_keys(
            repair,
            {
                'shift_uuid', 'payment_total_uuid', 'expected_before',
                'expected_after', 'counted_amount', 'difference_before',
                'difference_after', 'customer_change_excluded',
            },
            label,
        )
        shift_uuid = _uuid(repair['shift_uuid'], f'{label}.shift_uuid')
        row_uuid = _uuid(
            repair['payment_total_uuid'], f'{label}.payment_total_uuid',
        )
        if shift_uuid in seen_shifts:
            raise CommandError(f'{label}.shift_uuid is duplicated')
        if row_uuid in seen_totals:
            raise CommandError(f'{label}.payment_total_uuid is duplicated')
        seen_shifts.add(shift_uuid)
        seen_totals.add(row_uuid)

        before = _money(repair['expected_before'], f'{label}.expected_before')
        after = _money(repair['expected_after'], f'{label}.expected_after')
        counted = _money(
            repair['counted_amount'], f'{label}.counted_amount',
        )
        difference_before = _money(
            repair['difference_before'], f'{label}.difference_before',
        )
        difference_after = _money(
            repair['difference_after'], f'{label}.difference_after',
        )
        change = _money(
            repair['customer_change_excluded'],
            f'{label}.customer_change_excluded',
            positive=True,
        )
        if before - after != change:
            raise CommandError(
                f'{label}: expected_before - expected_after must equal '
                'customer_change_excluded'
            )
        if counted - before != difference_before:
            raise CommandError(
                f'{label}.difference_before must equal '
                'counted_amount - expected_before'
            )
        if counted - after != difference_after:
            raise CommandError(
                f'{label}.difference_after must equal '
                'counted_amount - expected_after'
            )
        normalized_shifts.append({
            'shift_uuid': shift_uuid,
            'payment_total_uuid': row_uuid,
            'expected_before': _money_text(before),
            'expected_after': _money_text(after),
            'counted_amount': _money_text(counted),
            'difference_before': _money_text(difference_before),
            'difference_after': _money_text(difference_after),
            'customer_change_excluded': _money_text(change),
        })

    normalized = {
        'version': 2,
        'repair_id': repair_id,
        'operator': operator,
        'branch_id': branch,
        'register_repair': {
            'register_uuid': register_uuid,
            'legacy_created_before': legacy_created_before,
            'expenses': sorted(normalized_expenses, key=lambda row: row['uuid']),
            'expected_expense_total': _money_text(expected_total),
        },
        'shift_payment_total_repairs': sorted(
            normalized_shifts, key=lambda row: row['shift_uuid'],
        ),
    }
    return normalized, _digest(normalized)


class Command(BaseCommand):
    help = (
        'Apply an explicit, forensically reviewed historical financial repair. '
        'Dry-run unless --apply is supplied.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--plan', required=True, help='Path to repair-plan JSON')
        parser.add_argument(
            '--branch', required=True,
            help='Exact branch_id; must match the plan',
        )
        parser.add_argument('--apply', action='store_true')
        parser.add_argument(
            '--expect-fingerprint',
            help='Evidence SHA-256 printed by a fresh dry-run',
        )
        parser.add_argument(
            '--confirm-repair-id',
            help='Must exactly repeat plan.repair_id when applying',
        )
        parser.add_argument(
            '--operator',
            required=True,
            help='Named operator/change ticket; must exactly match plan.operator',
        )

    def _atomic(self):
        """Indirection keeps commit-boundary reporting independently testable."""
        return transaction.atomic()

    def handle(self, *args, **options):
        plan, plan_sha = _load_plan(options['plan'])
        apply_changes = bool(options['apply'])
        mode = str(getattr(settings, 'DEPLOYMENT_MODE', '') or '').strip().lower()
        branch = str(options['branch'] or '').strip()

        if mode not in {'local', 'cloud'}:
            raise CommandError(
                'DEPLOYMENT_MODE must be local or cloud for this repair'
            )
        if branch != plan['branch_id']:
            raise CommandError('--branch must exactly match plan.branch_id')
        if str(options.get('operator') or '').strip() != plan['operator']:
            raise CommandError('--operator must exactly match plan.operator')
        if mode == 'local':
            configured_branch = str(
                getattr(settings, 'BRANCH_ID', '') or ''
            ).strip()
            if configured_branch != branch:
                raise CommandError(
                    'The local node BRANCH_ID does not match the repair branch'
                )

        if apply_changes:
            if options.get('confirm_repair_id') != plan['repair_id']:
                raise CommandError(
                    '--apply requires --confirm-repair-id exactly matching '
                    'plan.repair_id'
                )
            expected = str(options.get('expect_fingerprint') or '').lower()
            if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
                raise CommandError(
                    '--apply requires the 64-character --expect-fingerprint '
                    'from a fresh dry-run'
                )

        # Never print "Applied" from inside an atomic block. In particular,
        # PostgreSQL can reject a SERIALIZABLE transaction at COMMIT after all
        # statements succeeded. The success line is emitted only after __exit__
        # has returned without error.
        with self._atomic():
            result = self._execute(
                plan,
                plan_sha,
                mode=mode,
                apply_changes=apply_changes,
                expected_fingerprint=options.get('expect_fingerprint'),
            )

        self._write_preview(
            plan,
            plan_sha,
            mode,
            result['state'],
            result['fingerprint'],
        )
        if not apply_changes:
            self.stdout.write(self.style.WARNING('Dry-run only; no rows changed.'))
            return
        self.stdout.write(
            self.style.SUCCESS(
                f'Applied repair {plan["repair_id"]} on {mode}: '
                f'{result["writes"]} component write(s).'
            )
        )

    def _execute(
        self,
        plan,
        plan_sha,
        *,
        mode,
        apply_changes,
        expected_fingerprint,
    ):
        from base.models import AuditLog, SyncQueueRecord

        branch = plan['branch_id']
        if apply_changes:
            # Predicate reads of historical payment evidence must not race a
            # late sync insert. PostgreSQL SERIALIZABLE makes such a race abort
            # instead of committing a repair against a stale evidence set.
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SET TRANSACTION ISOLATION LEVEL SERIALIZABLE'
                    )

        state = self._inspect(
            plan,
            plan_sha,
            mode=mode,
            lock=apply_changes,
            require_cloud_predecessor=apply_changes,
        )
        fingerprint = _digest(state['fingerprint_evidence'])

        if not apply_changes:
            return {
                'state': state,
                'fingerprint': fingerprint,
                'writes': 0,
            }

        if fingerprint != str(expected_fingerprint).lower():
            raise CommandError(
                'Evidence fingerprint changed: expected '
                f'{str(expected_fingerprint).lower()}, found {fingerprint}; '
                'nothing changed.'
            )

        writes = 0
        created_audit_uuids = []
        register_was_written = False
        if mode == 'local' and not state['register']['already_applied']:
            from base.services.sync.config import SyncConfig

            if not SyncConfig.is_enabled():
                raise CommandError(
                    'Local repair requires synchronization to be enabled so '
                    'the corrected register and repair audit reach the cloud'
                )
            register = state['register']['row']
            expense_total = state['register']['expense_total']
            balance_before = register.current_balance
            balance_after = balance_before - expense_total
            register.current_balance = balance_after
            register.save(update_fields=['current_balance', 'last_updated'])
            audit = AuditLog.objects.create(
                action=AuditLog.Action.FINANCIAL_REPAIR,
                target_type='CashRegister',
                target_id=register.pk,
                branch_id=branch,
                metadata={
                    'repair_id': plan['repair_id'],
                    'plan_sha256': plan_sha,
                    'deployment_mode': mode,
                    'component': 'register',
                    'operator': plan['operator'],
                    'register_uuid': str(register.uuid),
                    'register_sync_version_after': register.sync_version,
                    'balance_before': _money_text(balance_before),
                    'balance_after': _money_text(balance_after),
                    'expense_count': len(state['register']['expenses']),
                    'expense_total': _money_text(expense_total),
                    'expense_fingerprint': state['register']['expense_fingerprint'],
                    'legacy_created_before':
                        plan['register_repair']['legacy_created_before'],
                    'expenses': [
                        {
                            'uuid': str(row.uuid),
                            'amount': _money_text(row.amount),
                            'shift_uuid': str(row.shift.uuid),
                            'created_at': _datetime_text(row.created_at),
                        }
                        for row in state['register']['expenses']
                    ],
                },
            )
            created_audit_uuids.append(audit.uuid)
            register_was_written = True
            writes += 1

        for item in state['shift_repairs']:
            if item['already_applied']:
                continue
            row = item['row']
            spec = item['spec']
            # ShiftPaymentTotal is append-only transport evidence. An ordinary
            # model save would bump sync_version, queue a branch rewrite that
            # the cloud correctly dead-letters, and publish a cloud rewrite the
            # terminal correctly refuses. This exceptional repair is executed
            # independently on each node from the same reviewed plan, with its
            # own AuditLog below. Preserve every sync/timestamp field and bypass
            # transport publication with one conditional maintenance UPDATE.
            updated = type(row)._base_manager.filter(
                pk=row.pk,
                expected_amount=Decimal(spec['expected_before']),
                counted_amount=Decimal(spec['counted_amount']),
                difference=Decimal(spec['difference_before']),
                confirmed_amount=Decimal('0.00'),
                sync_version=row.sync_version,
                synced_at=row.synced_at,
            ).update(
                expected_amount=Decimal(spec['expected_after']),
                difference=Decimal(spec['difference_after']),
            )
            if updated != 1:
                raise CommandError(
                    f'Payment total {row.uuid} changed after inspection; '
                    'nothing was repaired'
                )
            audit = AuditLog.objects.create(
                action=AuditLog.Action.FINANCIAL_REPAIR,
                target_type='ShiftPaymentTotal',
                target_id=row.pk,
                branch_id=branch,
                metadata={
                    'repair_id': plan['repair_id'],
                    'plan_sha256': plan_sha,
                    'deployment_mode': mode,
                    'component': 'shift_payment_total',
                    'operator': plan['operator'],
                    'shift_uuid': spec['shift_uuid'],
                    'payment_total_uuid': spec['payment_total_uuid'],
                    'method': 'CASH',
                    'expected_before': spec['expected_before'],
                    'expected_after': spec['expected_after'],
                    'counted_amount': spec['counted_amount'],
                    'difference_before': spec['difference_before'],
                    'difference_after': spec['difference_after'],
                    'customer_change_excluded':
                        spec['customer_change_excluded'],
                    'raw_cash_tender': item['raw_cash_tender'],
                    'refund_drawer_cash': item['refund_drawer_cash'],
                    'cash_expenses': item['cash_expenses'],
                    'legacy_raw_expected': item['legacy_raw_expected'],
                    'before_bundle_sha256': item['before_bundle_sha256'],
                },
            )
            created_audit_uuids.append(audit.uuid)
            writes += 1

        target_uuids = [
            item['row'].uuid for item in state['shift_repairs']
        ]
        unsafe_queue = SyncQueueRecord.objects.filter(
            model_name='shiftpaymenttotal',
            record_uuid__in=target_uuids,
        ).first()
        if unsafe_queue:
            raise CommandError(
                'Repair created or retained an unsafe ShiftPaymentTotal '
                f'outbound queue slot ({unsafe_queue.record_uuid}); rolling back'
            )
        from cashbox.models import ShiftPaymentTotal
        unsafe_rows = ShiftPaymentTotal._base_manager.filter(
            uuid__in=target_uuids,
            synced_at__isnull=True,
        )
        if unsafe_rows.exists():
            raise CommandError(
                'Repair left a ShiftPaymentTotal unpublished; rolling back'
            )

        if mode == 'local' and created_audit_uuids:
            queued_audits = SyncQueueRecord.objects.filter(
                model_name='auditlog',
                record_uuid__in=created_audit_uuids,
            ).count()
            if queued_audits != len(created_audit_uuids):
                raise CommandError(
                    'Local repair audit evidence was not durably queued for '
                    'cloud synchronization; rolling back'
                )
        if mode == 'local' and register_was_written:
            register = state['register']['row']
            if not SyncQueueRecord.objects.filter(
                model_name='cashregister',
                record_uuid=register.uuid,
            ).exists():
                raise CommandError(
                    'Corrected CashRegister was not durably queued for cloud '
                    'synchronization; rolling back'
                )

        return {
            'state': state,
            'fingerprint': fingerprint,
            'writes': writes,
        }

    def _inspect(
        self,
        plan,
        plan_sha,
        *,
        mode,
        lock,
        require_cloud_predecessor=False,
    ):
        from base.models import (
            AuditLog, CashReconciliation, CashRegister, OrderPayment,
            OrderRefund, Shift, SyncQueueRecord,
        )
        from django.db.models import Sum
        from cashbox.models import CashboxExpense, ShiftPaymentTotal
        from cashbox.services.drawer import _shift_orders, expected_payment_totals
        from core.shifts.service import (
            _build_settlement_manifest, settlement_manifest_digest,
        )

        branch = plan['branch_id']
        repair_id = plan['repair_id']

        shift_specs = plan['shift_payment_total_repairs']
        shift_uuids = [row['shift_uuid'] for row in shift_specs]
        payment_total_uuids = [row['payment_total_uuid'] for row in shift_specs]

        shifts_qs = Shift.objects.filter(uuid__in=shift_uuids).order_by('pk')
        totals_qs = ShiftPaymentTotal.objects.filter(
            uuid__in=payment_total_uuids,
        ).select_related('shift').order_by('pk')
        reconciliations_qs = CashReconciliation.objects.filter(
            shift__uuid__in=shift_uuids,
        ).order_by('pk')
        if lock:
            shifts_qs = shifts_qs.select_for_update()
            totals_qs = totals_qs.select_for_update()
            reconciliations_qs = reconciliations_qs.select_for_update()
        shifts = {str(row.uuid): row for row in shifts_qs}
        totals = {str(row.uuid): row for row in totals_qs}
        reconciled_shift_ids = set(
            reconciliations_qs.values_list('shift_id', flat=True)
        )

        if set(shifts) != set(shift_uuids):
            missing = sorted(set(shift_uuids) - set(shifts))
            raise CommandError(f'Plan shift UUID(s) not found: {", ".join(missing)}')
        if set(totals) != set(payment_total_uuids):
            missing = sorted(set(payment_total_uuids) - set(totals))
            raise CommandError(
                'Plan ShiftPaymentTotal UUID(s) not found: ' + ', '.join(missing)
            )

        marker_qs = AuditLog.objects.filter(
            action=_ACTION,
            branch_id=branch,
            metadata__repair_id=repair_id,
            metadata__deployment_mode=mode,
        ).order_by('pk')
        if lock:
            marker_qs = marker_qs.select_for_update()
        markers = list(marker_qs)
        if any(row.metadata.get('plan_sha256') != plan_sha for row in markers):
            raise CommandError(
                'Existing audit evidence uses this repair_id with a different '
                'plan SHA-256'
            )
        if any(row.metadata.get('operator') != plan['operator'] for row in markers):
            raise CommandError(
                'Existing audit evidence uses this repair_id with a different '
                'operator'
            )

        cloud_predecessor = None
        if mode == 'cloud':
            cloud_register_qs = CashRegister.objects.filter(
                branch_id=branch,
                is_deleted=False,
            ).order_by('pk')
            if lock:
                cloud_register_qs = cloud_register_qs.select_for_update()
            cloud_registers = list(cloud_register_qs)
            cloud_register = (
                cloud_registers[0] if len(cloud_registers) == 1 else None
            )
            predecessor_qs = AuditLog.objects.filter(
                action=_ACTION,
                branch_id=branch,
                metadata__repair_id=repair_id,
                metadata__deployment_mode='local',
            ).order_by('pk')
            if lock:
                predecessor_qs = predecessor_qs.select_for_update()
            predecessor_markers = list(predecessor_qs)
            cloud_predecessor = self._cloud_predecessor_evidence(
                plan,
                plan_sha,
                predecessor_markers,
                cloud_register=cloud_register,
                cloud_register_count=len(cloud_registers),
            )
            if require_cloud_predecessor and not cloud_predecessor['ready']:
                raise CommandError(
                    'Cloud apply requires the complete synchronized local '
                    'FINANCIAL_REPAIR audit chain for this exact plan: '
                    + cloud_predecessor['reason']
                )

        shift_results = []
        shift_fingerprint = []
        for spec in shift_specs:
            shift = shifts[spec['shift_uuid']]
            row = totals[spec['payment_total_uuid']]
            if row.shift_id != shift.pk:
                raise CommandError(
                    f'Payment total {row.uuid} does not belong to shift '
                    f'{shift.uuid}'
                )
            if shift.branch_id != branch or row.branch_id != branch:
                raise CommandError(
                    f'Shift/payment total {shift.uuid} is not owned by {branch}'
                )
            if shift.is_deleted or row.is_deleted:
                raise CommandError(
                    f'Shift/payment total {shift.uuid} is soft-deleted'
                )
            if shift.status != Shift.Status.ENDED:
                raise CommandError(
                    f'Shift {shift.uuid} is not in legacy ENDED state'
                )
            if shift.end_time is None:
                raise CommandError(f'Shift {shift.uuid} has no frozen end_time')
            if shift.treasury_settlement_eligible:
                raise CommandError(
                    f'Shift {shift.uuid} is treasury-settlement eligible; '
                    'legacy repair is forbidden'
                )
            if shift.settlement_manifest:
                raise CommandError(
                    f'Shift {shift.uuid} has an immutable close manifest; '
                    'legacy repair is forbidden'
                )
            if shift.pk in reconciled_shift_ids:
                raise CommandError(
                    f'Shift {shift.uuid} already has a manager reconciliation'
                )
            if row.method != 'CASH':
                raise CommandError(
                    f'Payment total {row.uuid} is not the CASH row'
                )
            if row.confirmed_amount != Decimal('0.00'):
                raise CommandError(
                    f'Payment total {row.uuid} already has manager confirmation'
                )
            if row.counted_amount != Decimal(spec['counted_amount']):
                raise CommandError(
                    f'Payment total {row.uuid} counted_amount no longer '
                    'matches the plan'
                )
            if row.synced_at is None:
                raise CommandError(
                    f'Payment total {row.uuid} has not completed its original '
                    'synchronization; historical rewrite is forbidden'
                )
            if SyncQueueRecord.objects.filter(
                model_name='shiftpaymenttotal',
                record_uuid=row.uuid,
            ).exists():
                raise CommandError(
                    f'Payment total {row.uuid} still has an outbound sync '
                    'queue slot; historical rewrite is forbidden'
                )

            before_state = (
                row.expected_amount == Decimal(spec['expected_before'])
                and row.difference == Decimal(spec['difference_before'])
            )
            after_state = (
                row.expected_amount == Decimal(spec['expected_after'])
                and row.difference == Decimal(spec['difference_after'])
            )
            if before_state == after_state:
                raise CommandError(
                    f'Payment total {row.uuid} matches neither exactly one '
                    'reviewed before/after state'
                )

            canonical = expected_payment_totals(shift).get('CASH')
            if canonical != Decimal(spec['expected_after']):
                raise CommandError(
                    f'Canonical CASH for shift {shift.uuid} is '
                    f'{_money_text(canonical)}, not reviewed expected_after '
                    f'{spec["expected_after"]}'
                )

            # Prove the historical defect directly. The legacy row summed the
            # raw CASH tender lines (which include customer change), while the
            # canonical drawer derives bill cash. Refund and expense deductions
            # are applied identically on both sides so the remaining delta can
            # only be raw customer change represented by the reviewed plan.
            orders = _shift_orders(shift)
            raw_cash_tender = (
                OrderPayment.objects.filter(
                    order__in=orders,
                    branch_id=branch,
                    is_deleted=False,
                    method='CASH',
                ).aggregate(total=Sum('amount'))['total']
                or Decimal('0.00')
            )
            refund_drawer_cash = (
                OrderRefund.objects.filter(
                    shift=shift,
                    branch_id=branch,
                    is_deleted=False,
                ).aggregate(total=Sum('drawer_cash_amount'))['total']
                or Decimal('0.00')
            )
            cash_expenses = (
                CashboxExpense.objects.filter(
                    shift=shift,
                    branch_id=branch,
                    is_deleted=False,
                ).aggregate(total=Sum('amount'))['total']
                or Decimal('0.00')
            )
            legacy_raw_expected = (
                raw_cash_tender - refund_drawer_cash - cash_expenses
            )
            if legacy_raw_expected != Decimal(spec['expected_before']):
                raise CommandError(
                    f'Raw tender evidence for shift {shift.uuid} proves '
                    f'{_money_text(legacy_raw_expected)}, not reviewed '
                    f'expected_before {spec["expected_before"]}'
                )
            if (
                legacy_raw_expected - canonical
                != Decimal(spec['customer_change_excluded'])
            ):
                raise CommandError(
                    f'Raw tender evidence for shift {shift.uuid} does not '
                    'prove the reviewed customer-change exclusion'
                )

            all_rows = list(
                ShiftPaymentTotal.objects.filter(
                    shift=shift, is_deleted=False,
                ).order_by('method')
            )
            evidence_digest = settlement_manifest_digest(
                _build_settlement_manifest(shift, all_rows)
            )
            matching_markers = [
                marker for marker in markers
                if marker.metadata.get('component') == 'shift_payment_total'
                and marker.metadata.get('payment_total_uuid') == str(row.uuid)
            ]
            if len(matching_markers) > 1:
                raise CommandError(
                    f'Duplicate repair audit markers for payment total {row.uuid}'
                )
            if after_state and not matching_markers:
                raise CommandError(
                    f'Payment total {row.uuid} is corrected but lacks matching '
                    'repair audit evidence'
                )
            if before_state and matching_markers:
                raise CommandError(
                    f'Payment total {row.uuid} is stale despite an applied '
                    'repair audit marker'
                )
            if matching_markers:
                marker = matching_markers[0]
                metadata = marker.metadata
                expected_marker = {
                    'shift_uuid': spec['shift_uuid'],
                    'payment_total_uuid': spec['payment_total_uuid'],
                    'expected_before': spec['expected_before'],
                    'expected_after': spec['expected_after'],
                    'counted_amount': spec['counted_amount'],
                    'difference_before': spec['difference_before'],
                    'difference_after': spec['difference_after'],
                    'customer_change_excluded':
                        spec['customer_change_excluded'],
                    'raw_cash_tender': _money_text(raw_cash_tender),
                    'refund_drawer_cash': _money_text(refund_drawer_cash),
                    'cash_expenses': _money_text(cash_expenses),
                    'legacy_raw_expected': _money_text(legacy_raw_expected),
                }
                if (
                    marker.target_type != 'ShiftPaymentTotal'
                    or marker.target_id != row.pk
                    or any(
                        metadata.get(key) != value
                        for key, value in expected_marker.items()
                    )
                ):
                    raise CommandError(
                        f'Repair audit marker for payment total {row.uuid} '
                        'does not bind the current row and reviewed evidence'
                    )

            already_applied = bool(matching_markers)
            shift_results.append({
                'spec': spec,
                'shift': shift,
                'row': row,
                'already_applied': already_applied,
                'before_bundle_sha256': evidence_digest,
                'raw_cash_tender': _money_text(raw_cash_tender),
                'refund_drawer_cash': _money_text(refund_drawer_cash),
                'cash_expenses': _money_text(cash_expenses),
                'legacy_raw_expected': _money_text(legacy_raw_expected),
            })
            shift_fingerprint.append({
                'shift_uuid': str(shift.uuid),
                'payment_total_uuid': str(row.uuid),
                'status': shift.status,
                'end_time': shift.end_time.isoformat(),
                'expected_amount': _money_text(row.expected_amount),
                'counted_amount': _money_text(row.counted_amount),
                'confirmed_amount': _money_text(row.confirmed_amount),
                'difference': _money_text(row.difference),
                'canonical_cash': _money_text(canonical),
                'raw_cash_tender': _money_text(raw_cash_tender),
                'refund_drawer_cash': _money_text(refund_drawer_cash),
                'cash_expenses': _money_text(cash_expenses),
                'legacy_raw_expected': _money_text(legacy_raw_expected),
                'bundle_sha256': evidence_digest,
                'already_applied': already_applied,
            })

        register_state = None
        register_fingerprint = None
        if mode == 'local':
            register_qs = CashRegister.objects.filter(
                branch_id=branch, is_deleted=False,
            ).order_by('pk')
            if lock:
                register_qs = register_qs.select_for_update()
            registers = list(register_qs)
            if len(registers) != 1:
                raise CommandError(
                    f'Expected exactly one live CashRegister for {branch}; '
                    f'found {len(registers)}'
                )
            register = registers[0]
            if str(register.uuid) != plan['register_repair']['register_uuid']:
                raise CommandError(
                    f'Live CashRegister UUID {register.uuid} does not match '
                    'the forensically reviewed register UUID '
                    f'{plan["register_repair"]["register_uuid"]}'
                )

            expense_specs = plan['register_repair']['expenses']
            expense_uuids = [row['uuid'] for row in expense_specs]
            legacy_created_before = parse_datetime(
                plan['register_repair']['legacy_created_before']
            )
            expenses_qs = CashboxExpense.objects.filter(
                uuid__in=expense_uuids,
            ).select_related('shift').order_by('uuid')
            if lock:
                expenses_qs = expenses_qs.select_for_update()
            expenses = list(expenses_qs)
            by_uuid = {str(row.uuid): row for row in expenses}
            if set(by_uuid) != set(expense_uuids):
                missing = sorted(set(expense_uuids) - set(by_uuid))
                raise CommandError(
                    'Plan CashboxExpense UUID(s) not found: ' + ', '.join(missing)
                )
            for spec in expense_specs:
                expense = by_uuid[spec['uuid']]
                expense_shift = expense.shift
                if (
                    expense.is_deleted
                    or expense.branch_id != branch
                    or expense.amount != Decimal(spec['amount'])
                    or expense.amount <= 0
                ):
                    raise CommandError(
                        f'CashboxExpense {expense.uuid} no longer matches the '
                        'reviewed live branch/amount state'
                    )
                if expense.register_command:
                    raise CommandError(
                        f'CashboxExpense {expense.uuid} is a remote register '
                        'command and cannot be part of this legacy repair'
                    )
                if (
                    str(expense_shift.uuid) != spec['shift_uuid']
                    or expense_shift.branch_id != branch
                    or expense_shift.is_deleted
                    or expense_shift.status not in (
                        Shift.Status.ENDED, Shift.Status.COMPLETED,
                    )
                    or expense_shift.end_time is None
                ):
                    raise CommandError(
                        f'CashboxExpense {expense.uuid} is not bound to the '
                        'reviewed closed branch shift'
                    )
                if _datetime_text(expense.created_at) != spec['created_at']:
                    raise CommandError(
                        f'CashboxExpense {expense.uuid} created_at no longer '
                        'matches the reviewed evidence'
                    )
                if (
                    expense.created_at >= legacy_created_before
                    or expense_shift.end_time >= legacy_created_before
                ):
                    raise CommandError(
                        f'CashboxExpense {expense.uuid} is not strictly before '
                        'the reviewed legacy cutoff'
                    )
                if (
                    expense.created_at < expense_shift.start_time
                    or expense.created_at > expense_shift.end_time
                ):
                    raise CommandError(
                        f'CashboxExpense {expense.uuid} falls outside its '
                        'reviewed closed shift window'
                    )
            expenses = [by_uuid[row['uuid']] for row in expense_specs]
            expense_total = sum(
                (row.amount for row in expenses), Decimal('0.00'),
            )
            if expense_total != Decimal(
                plan['register_repair']['expected_expense_total']
            ):
                raise CommandError(
                    'Database expense total no longer matches the reviewed plan'
                )
            expense_evidence = [
                {
                    'uuid': str(row.uuid),
                    'amount': _money_text(row.amount),
                    'shift_uuid': str(row.shift.uuid),
                    'created_at': _datetime_text(row.created_at),
                }
                for row in expenses
            ]
            expense_fingerprint = _digest(expense_evidence)
            register_markers = [
                marker for marker in markers
                if marker.metadata.get('component') == 'register'
            ]
            if len(register_markers) > 1:
                raise CommandError('Duplicate repair audit markers for register')

            # A CashboxExpense may authorize a register deduction once, across
            # every repair id. Repair-id-scoped idempotency alone would permit
            # a second plan to deduct the same historical payout again.
            all_register_marker_qs = AuditLog.objects.filter(
                action=_ACTION,
                branch_id=branch,
                metadata__component='register',
            ).order_by('pk')
            if lock:
                all_register_marker_qs = (
                    all_register_marker_qs.select_for_update()
                )
            planned_expenses = set(expense_uuids)
            current_marker_ids = {row.pk for row in register_markers}
            for prior in all_register_marker_qs:
                if prior.pk in current_marker_ids:
                    continue
                prior_expenses = {
                    str(item.get('uuid'))
                    for item in (prior.metadata.get('expenses') or [])
                    if isinstance(item, dict) and item.get('uuid')
                }
                overlap = sorted(planned_expenses & prior_expenses)
                if overlap:
                    raise CommandError(
                        'CashboxExpense UUID(s) already consumed by another '
                        'financial repair '
                        f'{prior.metadata.get("repair_id") or "UNKNOWN"}: '
                        + ', '.join(overlap)
                    )

            already_applied = bool(register_markers)
            if not already_applied and register.current_balance < expense_total:
                raise CommandError(
                    'CashRegister balance is below the reviewed historical '
                    'expense deduction'
                )
            if already_applied:
                marker = register_markers[0]
                if (
                    marker.target_type != 'CashRegister'
                    or marker.target_id != register.pk
                    or marker.metadata.get('register_uuid')
                    != str(register.uuid)
                    or marker.metadata.get('expense_fingerprint')
                    != expense_fingerprint
                    or marker.metadata.get('expense_total')
                    != _money_text(expense_total)
                    or marker.metadata.get('legacy_created_before')
                    != plan['register_repair']['legacy_created_before']
                    or not isinstance(
                        marker.metadata.get('register_sync_version_after'), int,
                    )
                ):
                    raise CommandError(
                        'Register repair audit marker does not match current '
                        'reviewed expense evidence'
                    )
            register_state = {
                'row': register,
                'expenses': expenses,
                'expense_total': expense_total,
                'expense_fingerprint': expense_fingerprint,
                'already_applied': already_applied,
            }
            register_fingerprint = {
                'register_uuid': str(register.uuid),
                'current_balance': _money_text(register.current_balance),
                'remote_cash_out_applied_total':
                    _money_text(register.remote_cash_out_applied_total),
                'expense_count': len(expenses),
                'expense_total': _money_text(expense_total),
                'expense_fingerprint': expense_fingerprint,
                'legacy_created_before':
                    plan['register_repair']['legacy_created_before'],
                'already_applied': already_applied,
            }

        known_marker_ids = {
            marker.pk
            for item in shift_results
            for marker in markers
            if marker.metadata.get('component') == 'shift_payment_total'
            and marker.metadata.get('payment_total_uuid')
            == item['spec']['payment_total_uuid']
        }
        if mode == 'local':
            known_marker_ids.update(
                marker.pk for marker in markers
                if marker.metadata.get('component') == 'register'
            )
        unexpected = [marker for marker in markers if marker.pk not in known_marker_ids]
        if unexpected:
            raise CommandError(
                'Existing repair audit contains an unexpected component for '
                'this repair_id/deployment_mode'
            )

        fingerprint_evidence = {
            'plan_sha256': plan_sha,
            'deployment_mode': mode,
            'branch_id': branch,
            'register': register_fingerprint,
            'shift_payment_totals': shift_fingerprint,
            'local_predecessor': cloud_predecessor,
        }
        return {
            'register': register_state,
            'shift_repairs': shift_results,
            'cloud_predecessor': cloud_predecessor,
            'fingerprint_evidence': fingerprint_evidence,
        }

    @staticmethod
    def _cloud_predecessor_evidence(
        plan,
        plan_sha,
        markers,
        *,
        cloud_register,
        cloud_register_count,
    ):
        """Validate the branch-side repair chain before touching cloud copies."""
        expected_components = {
            ('register', plan['register_repair']['register_uuid']),
            *{
                ('shift_payment_total', row['payment_total_uuid'])
                for row in plan['shift_payment_total_repairs']
            },
        }
        evidence = []
        actual_components = []
        reasons = []
        seen = set()

        for marker in markers:
            metadata = marker.metadata or {}
            component = metadata.get('component')
            component_id = (
                metadata.get('register_uuid')
                if component == 'register'
                else metadata.get('payment_total_uuid')
            )
            key = (component, component_id)
            actual_components.append(key)
            if key in seen:
                reasons.append(f'duplicate local marker {component}:{component_id}')
            seen.add(key)
            if metadata.get('plan_sha256') != plan_sha:
                reasons.append('local marker plan SHA-256 mismatch')
            if metadata.get('operator') != plan['operator']:
                reasons.append('local marker operator mismatch')
            if marker.synced_at is None:
                reasons.append(
                    f'local marker {component}:{component_id} is not synchronized'
                )
            evidence.append({
                'uuid': str(marker.uuid),
                'component': component,
                'component_id': component_id,
                'synced_at': (
                    _datetime_text(marker.synced_at)
                    if marker.synced_at is not None
                    else None
                ),
                'plan_sha256': metadata.get('plan_sha256'),
            })

        if set(actual_components) != expected_components:
            missing = sorted(expected_components - set(actual_components))
            unexpected = sorted(set(actual_components) - expected_components)
            if missing:
                reasons.append(
                    'missing local marker(s): '
                    + ', '.join(f'{kind}:{identifier}' for kind, identifier in missing)
                )
            if unexpected:
                reasons.append(
                    'unexpected local marker(s): '
                    + ', '.join(
                        f'{kind}:{identifier}' for kind, identifier in unexpected
                    )
                )

        # Bind every local shift marker to the exact money transition reviewed
        # in the plan, not only to its row UUID.
        by_payment_uuid = {
            marker.metadata.get('payment_total_uuid'): marker
            for marker in markers
            if marker.metadata.get('component') == 'shift_payment_total'
        }
        for spec in plan['shift_payment_total_repairs']:
            marker = by_payment_uuid.get(spec['payment_total_uuid'])
            if marker is None:
                continue
            metadata = marker.metadata
            for key in (
                'shift_uuid', 'payment_total_uuid', 'expected_before',
                'expected_after', 'counted_amount', 'difference_before',
                'difference_after', 'customer_change_excluded',
            ):
                if metadata.get(key) != spec[key]:
                    reasons.append(
                        f'local marker {spec["payment_total_uuid"]} '
                        f'{key} mismatch'
                    )

        register_markers = [
            marker for marker in markers
            if marker.metadata.get('component') == 'register'
        ]
        if len(register_markers) == 1:
            register_marker = register_markers[0]
            metadata = register_marker.metadata
            if (
                register_marker.target_type != 'CashRegister'
                or metadata.get('register_uuid')
                != plan['register_repair']['register_uuid']
                or metadata.get('expense_total')
                != plan['register_repair']['expected_expense_total']
                or metadata.get('legacy_created_before')
                != plan['register_repair']['legacy_created_before']
            ):
                reasons.append('local register marker evidence mismatch')
            try:
                balance_before = _money(
                    metadata.get('balance_before'),
                    'local register marker balance_before',
                )
                balance_after = _money(
                    metadata.get('balance_after'),
                    'local register marker balance_after',
                )
            except CommandError as exc:
                reasons.append(str(exc))
            else:
                if (
                    balance_before
                    - Decimal(plan['register_repair']['expected_expense_total'])
                    != balance_after
                ):
                    reasons.append('local register marker balance math mismatch')

            marker_version = metadata.get('register_sync_version_after')
            if (
                not isinstance(marker_version, int)
                or isinstance(marker_version, bool)
                or marker_version < 1
            ):
                reasons.append('local register marker sync version is invalid')
            if cloud_register_count != 1 or cloud_register is None:
                reasons.append(
                    'cloud does not have exactly one live branch CashRegister'
                )
            elif (
                str(cloud_register.uuid)
                != plan['register_repair']['register_uuid']
                or _money_text(cloud_register.current_balance)
                != metadata.get('balance_after')
                or cloud_register.sync_version != marker_version
                or cloud_register.synced_at is None
            ):
                reasons.append(
                    'cloud CashRegister has not acknowledged the exact '
                    'corrected branch generation'
                )

        unique_reasons = sorted(set(reasons))
        return {
            'ready': not unique_reasons,
            'reason': '; '.join(unique_reasons) if unique_reasons else 'ready',
            'marker_count': len(markers),
            'cloud_register': (
                {
                    'uuid': str(cloud_register.uuid),
                    'current_balance':
                        _money_text(cloud_register.current_balance),
                    'sync_version': cloud_register.sync_version,
                    'synced_at': (
                        _datetime_text(cloud_register.synced_at)
                        if cloud_register.synced_at is not None
                        else None
                    ),
                }
                if cloud_register is not None
                else {'count': cloud_register_count}
            ),
            'markers': sorted(
                evidence,
                key=lambda row: (
                    str(row['component']), str(row['component_id']), row['uuid'],
                ),
            ),
        }

    def _write_preview(self, plan, plan_sha, mode, state, fingerprint):
        self.stdout.write(
            f'Repair id: {plan["repair_id"]}; mode: {mode}; '
            f'branch: {plan["branch_id"]}; operator: {plan["operator"]}'
        )
        self.stdout.write(f'Plan SHA-256: {plan_sha}')
        self.stdout.write(f'Evidence fingerprint: {fingerprint}')
        if mode == 'local':
            register = state['register']
            row = register['row']
            if register['already_applied']:
                self.stdout.write(
                    'Register repair: already applied (audit marker present).'
                )
            else:
                after = row.current_balance - register['expense_total']
                self.stdout.write(
                    'Register repair: {count} expense(s), {total}; '
                    'balance {before} -> {after}'.format(
                        count=len(register['expenses']),
                        total=_money_text(register['expense_total']),
                        before=_money_text(row.current_balance),
                        after=_money_text(after),
                    )
                )
        else:
            self.stdout.write(
                'Register repair: skipped on cloud (till-owned cursor).'
            )
            predecessor = state['cloud_predecessor']
            readiness = 'READY' if predecessor['ready'] else 'NOT READY'
            self.stdout.write(
                'Cloud prerequisite — synchronized local repair audit: '
                f'{readiness} ({predecessor["reason"]}).'
            )
        for item in state['shift_repairs']:
            spec = item['spec']
            status = 'already applied' if item['already_applied'] else 'candidate'
            self.stdout.write(
                'Shift {shift}; CASH row {row}; {status}; expected '
                '{before} -> {after}; difference {diff_before} -> {diff_after}; '
                'customer change excluded {change}'.format(
                    shift=spec['shift_uuid'],
                    row=spec['payment_total_uuid'],
                    status=status,
                    before=spec['expected_before'],
                    after=spec['expected_after'],
                    diff_before=spec['difference_before'],
                    diff_after=spec['difference_after'],
                    change=spec['customer_change_excluded'],
                )
            )
