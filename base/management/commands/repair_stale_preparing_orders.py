"""Conservatively retire historical PREPARING orders on the cloud hub.

This command is intentionally narrower than a generic status cleanup.  A paid
order is eligible only when its payment belongs to a shift that has already
ended, and that shift has been closed for the configured grace period.  Active
kitchen work and orders without an auditable shift boundary are never touched.
"""

import hashlib
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Exists, Max, OuterRef, Q
from django.utils import timezone


class Command(BaseCommand):
    help = (
        'Move paid, stale PREPARING orders from closed shifts to READY. '
        'Dry-run unless --apply is supplied.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--branch', help='Limit candidates to one branch_id')
        parser.add_argument(
            '--older-than-hours', type=int, default=6,
            help='Require the owning shift to have ended this many hours ago '
                 '(default: 6)',
        )
        parser.add_argument('--expect-count', type=int)
        parser.add_argument('--expect-total')
        parser.add_argument(
            '--expect-fingerprint',
            help='SHA-256 of the sorted candidate UUIDs printed by dry-run',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from base.models import Order, OrderItem, OrderRefund, Shift

        apply_changes = options['apply']
        branch = str(options.get('branch') or '').strip()
        grace_hours = options['older_than_hours']

        if grace_hours < 1:
            raise CommandError('--older-than-hours must be at least 1')
        if apply_changes and getattr(settings, 'DEPLOYMENT_MODE', '') != 'cloud':
            raise CommandError(
                '--apply is restricted to the cloud collector; a till must '
                'remain authoritative for its live kitchen queue.'
            )
        if apply_changes and not branch:
            raise CommandError('--apply requires an explicit --branch')
        if apply_changes and (
            options.get('expect_count') is None
            or not options.get('expect_fingerprint')
        ):
            raise CommandError(
                '--apply requires --expect-count and --expect-fingerprint '
                'from a fresh dry-run'
            )

        closed_before = timezone.now() - timedelta(hours=grace_hours)
        owning_closed_shift = Shift.objects.filter(
            is_deleted=False,
            status__in=(Shift.Status.ENDED, Shift.Status.COMPLETED),
            user_id=OuterRef('cashier_id'),
            branch_id=OuterRef('branch_id'),
            start_time__lte=OuterRef('paid_at'),
            end_time__gte=OuterRef('paid_at'),
            end_time__lte=closed_before,
        )
        has_live_item = OrderItem.objects.filter(
            order_id=OuterRef('pk'), is_deleted=False,
        )
        has_refund = OrderRefund.objects.filter(
            order_id=OuterRef('pk'), is_deleted=False,
        )

        orders = Order.objects.filter(
            is_deleted=False,
            status=Order.Status.PREPARING,
            is_paid=True,
            paid_at__isnull=False,
            cashier_id__isnull=False,
        ).annotate(
            _closed_shift=Exists(owning_closed_shift),
            _has_live_item=Exists(has_live_item),
            _has_refund=Exists(has_refund),
        ).filter(
            _closed_shift=True,
            _has_live_item=True,
            _has_refund=False,
        ).order_by('pk')
        if branch:
            orders = orders.filter(branch_id=branch)
        if apply_changes:
            orders = orders.select_for_update()

        candidates = list(orders)
        candidate_total = sum(
            (order.total_amount for order in candidates), Decimal('0'),
        )
        candidate_uuids = sorted(str(order.uuid) for order in candidates)
        fingerprint = hashlib.sha256(
            '\n'.join(candidate_uuids).encode('ascii')
        ).hexdigest()

        self._check_expectations(
            candidates, candidate_total, fingerprint, options,
        )

        self.stdout.write(
            'Candidates: {count}; total: {total}; fingerprint: {fingerprint}; '
            'closed_before: {closed_before}'.format(
                count=len(candidates),
                total=f'{candidate_total:.2f}',
                fingerprint=fingerprint,
                closed_before=closed_before.isoformat(),
            )
        )
        for order_uuid in candidate_uuids:
            self.stdout.write(order_uuid)

        if not apply_changes:
            self.stdout.write(self.style.WARNING('Dry-run only; no rows changed.'))
            return

        repaired_with_evidence_time = 0
        for order in candidates:
            # If every live line has a real ready timestamp, the latest one is
            # authoritative evidence for the header.  Otherwise do not invent
            # a preparation time: READY here retires an old operational queue
            # row, while ready_at=NULL keeps prep-time analytics honest.
            item_evidence = OrderItem.objects.filter(
                order=order, is_deleted=False,
            ).aggregate(
                total=Count('id'),
                ready=Count('id', filter=Q(ready_at__isnull=False)),
                latest=Max('ready_at'),
            )
            order.status = Order.Status.READY
            if (
                order.ready_at is None
                and item_evidence['total']
                and item_evidence['ready'] == item_evidence['total']
            ):
                order.ready_at = item_evidence['latest']
                repaired_with_evidence_time += 1

            # A model save is required.  QuerySet.update() would bypass
            # SyncMixin's version bump and cloud publication, leaving the till
            # with the stale PREPARING value.
            order.save()

        self.stdout.write(
            self.style.SUCCESS(
                f'Repaired {len(candidates)} stale PREPARING order(s), '
                f'{candidate_total:.2f} total; '
                f'{repaired_with_evidence_time} ready_at value(s) restored '
                'from complete item evidence.'
            )
        )

    @staticmethod
    def _check_expectations(candidates, total, fingerprint, options):
        expected_count = options.get('expect_count')
        if expected_count is not None and len(candidates) != expected_count:
            raise CommandError(
                f'Candidate count changed: expected {expected_count}, '
                f'found {len(candidates)}; nothing changed.'
            )

        expected_total = options.get('expect_total')
        if expected_total is not None:
            try:
                expected_total = Decimal(expected_total)
            except (InvalidOperation, TypeError):
                raise CommandError('--expect-total must be a decimal number')
            if total != expected_total:
                raise CommandError(
                    f'Candidate total changed: expected {expected_total:.2f}, '
                    f'found {total:.2f}; nothing changed.'
                )

        expected_fingerprint = options.get('expect_fingerprint')
        if expected_fingerprint and fingerprint != expected_fingerprint.lower():
            raise CommandError(
                'Candidate UUID fingerprint changed: expected '
                f'{expected_fingerprint.lower()}, found {fingerprint}; '
                'nothing changed.'
            )
