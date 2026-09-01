import json
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from base.models import (
    PaymentMethodConfig, TreasuryAccount, TreasuryTransaction, User,
)
from base.money import uzs_int
from base.security.permissions import user_has_permission
from base.services.treasury_service import _apply, _lock_accounts


class Command(BaseCommand):
    help = 'Audit or explicitly apply append-only legacy tender reclassification.'

    def add_arguments(self, parser):
        parser.add_argument('--branch', required=True)
        parser.add_argument('--cutoff', required=True)
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument(
            '--approve-transaction',
            action='append',
            type=int,
            default=[],
        )
        parser.add_argument('--actor-id', type=int)
        parser.add_argument('--reason')

    def handle(self, *args, **options):
        try:
            cutoff = datetime.fromisoformat(options['cutoff'])
        except ValueError as exc:
            raise CommandError('--cutoff must be an ISO-8601 datetime') from exc
        if timezone.is_naive(cutoff):
            cutoff = timezone.make_aware(cutoff, timezone.get_current_timezone())
        branch_id = options['branch'].strip()
        if not options['dry_run'] and not options['approve_transaction']:
            raise CommandError(
                'Use --dry-run or explicitly repeat --approve-transaction ID.'
            )
        actor = None
        if not options['dry_run']:
            if not options['actor_id'] or not str(options.get('reason') or '').strip():
                raise CommandError('--actor-id and --reason are required when applying.')
            actor = User.objects.filter(
                pk=options['actor_id'],
                status=User.UserStatus.ACTIVE,
                is_deleted=False,
            ).first()
            if actor is None or not user_has_permission(
                actor, 'money.control.reconcile',
            ):
                raise CommandError('Actor lacks money.control.reconcile permission.')
        methods = {'CARD', 'UZCARD', 'HUMO', 'PAYME'}
        methods.update(PaymentMethodConfig.objects.filter(
            treasury_destination='BANK',
        ).values_list('code', flat=True))
        candidates = list(TreasuryTransaction.objects.filter(
            branch_id=branch_id,
            is_deleted=False,
            type=TreasuryTransaction.Type.SHIFT_DEPOSIT,
            reference_type='ShiftSettlement',
            account__kind=TreasuryAccount.Kind.SAFE,
            category__in=methods,
            created_at__lte=cutoff,
        ).select_related('account').order_by('id'))
        approved = set(options['approve_transaction'])
        report = []
        safe_ids = set()
        applied = []
        for row in candidates:
            already = TreasuryTransaction.objects.filter(
                branch_id=branch_id,
                reference_type='LegacyShiftReclassification',
                reference_id=row.id,
            ).exists()
            window_start = row.created_at - timedelta(days=2)
            window_end = row.created_at + timedelta(days=2)
            related = list(TreasuryTransaction.objects.filter(
                branch_id=branch_id,
                is_deleted=False,
                type=TreasuryTransaction.Type.TRANSFER_OUT,
                account__kind=TreasuryAccount.Kind.SAFE,
                delta=-row.delta,
                created_at__gte=window_start,
                created_at__lte=window_end,
            ).order_by('id').values_list('id', flat=True))
            safe = not already and not related and row.delta > 0
            classification = (
                'ALREADY_RECLASSIFIED' if already
                else ('SAFE' if safe else 'AMBIGUOUS')
            )
            candidate = {
                'treasury_transaction_id': row.id,
                'shift_id': row.reference_id,
                'method': row.category,
                'amount_uzs': uzs_int(row.delta),
                'possible_related_transfer_ids': related,
                'classification': classification,
                'approved': row.id in approved,
            }
            report.append(candidate)
            if safe:
                safe_ids.add(row.id)
        candidate_ids = {row.id for row in candidates}
        unknown_approvals = approved - candidate_ids
        if unknown_approvals:
            raise CommandError(
                f'Approved IDs are not eligible candidates: {sorted(unknown_approvals)}'
            )
        ambiguous_approvals = approved - safe_ids
        if ambiguous_approvals:
            raise CommandError(
                f'Approved IDs are ambiguous and were not changed: '
                f'{sorted(ambiguous_approvals)}'
            )
        if not options['dry_run'] and approved:
            with transaction.atomic():
                for row in candidates:
                    if row.id not in approved:
                        continue
                    original = TreasuryTransaction.objects.select_for_update().get(
                        pk=row.id,
                        branch_id=branch_id,
                    )
                    if TreasuryTransaction.objects.filter(
                        branch_id=branch_id,
                        reference_type='LegacyShiftReclassification',
                        reference_id=original.id,
                    ).exists():
                        raise CommandError(
                            f'Transaction {original.id} was already reclassified.'
                        )
                    accounts = _lock_accounts(
                        [TreasuryAccount.Kind.SAFE, TreasuryAccount.Kind.BANK],
                        branch_id,
                    )
                    if accounts[TreasuryAccount.Kind.SAFE].balance < original.delta:
                        raise CommandError(
                            f'SAFE balance cannot fund reclassification {row.id}.'
                        )
                    debit = _apply(
                        accounts[TreasuryAccount.Kind.SAFE],
                        -original.delta,
                        TreasuryTransaction.Type.SHIFT_RECLASS_OUT,
                        category=original.category,
                        description=(
                            f'Approved reclassification of legacy deposit {row.id}: '
                            f'{str(options["reason"]).strip()}'
                        ),
                        reference_type='LegacyShiftReclassification',
                        reference_id=row.id,
                        branch_id=branch_id,
                        performed_by=actor,
                    )
                    credit = _apply(
                        accounts[TreasuryAccount.Kind.BANK],
                        original.delta,
                        TreasuryTransaction.Type.SHIFT_RECLASS_IN,
                        category=original.category,
                        description=(
                            f'Approved reclassification of legacy deposit {row.id}: '
                            f'{str(options["reason"]).strip()}'
                        ),
                        reference_type='LegacyShiftReclassification',
                        reference_id=row.id,
                        branch_id=branch_id,
                        performed_by=actor,
                    )
                    applied.append({
                        'original_id': row.id,
                        'safe_debit_id': debit.id,
                        'bank_credit_id': credit.id,
                    })
        self.stdout.write(json.dumps({
            'command': 'reclassify_legacy_shift_tenders',
            'branch_id': branch_id,
            'cutoff': cutoff.isoformat(),
            'dry_run': options['dry_run'],
            'actor_id': actor.id if actor else None,
            'candidate_count': len(report),
            'candidates': report,
            'applied': applied,
        }, ensure_ascii=False, indent=2))
