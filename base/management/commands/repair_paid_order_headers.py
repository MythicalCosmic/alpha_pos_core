import hashlib
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Exists, OuterRef


class Command(BaseCommand):
    help = (
        'Repair stale unpaid Order headers only when a later, single, exact '
        'OrderPayment proves that the paid header mutation was lost in sync. '
        'Dry-run unless --apply is supplied.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--branch', help='Limit candidates to one branch_id')
        parser.add_argument('--expect-count', type=int)
        parser.add_argument('--expect-total')
        parser.add_argument(
            '--expect-fingerprint',
            help='SHA-256 of the sorted candidate UUIDs printed by dry-run',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from base.models import Order, OrderItem, OrderPayment

        apply_changes = options['apply']
        if apply_changes and getattr(settings, 'DEPLOYMENT_MODE', '') != 'cloud':
            raise CommandError(
                '--apply is restricted to the cloud collector; a till must '
                're-push its authoritative order instead.'
            )

        # Narrow in SQL before taking any row locks. A production till can have
        # many legitimate unpaid/open tickets; locking all of them for the full
        # evidence scan would block active checkout and sync writes. Dry-runs do
        # not lock at all. Apply locks only headers that have payment evidence.
        has_payment = OrderPayment.objects.filter(order_id=OuterRef('pk'))
        orders = Order.objects.filter(
            is_deleted=False,
            is_paid=False,
            payment_method__isnull=True,
            paid_at__isnull=True,
        ).exclude(status=Order.Status.CANCELED).annotate(
            _has_payment=Exists(has_payment),
        ).filter(_has_payment=True).order_by('pk')
        if options.get('branch'):
            orders = orders.filter(branch_id=options['branch'])
        if apply_changes:
            orders = orders.select_for_update()

        concrete_methods = {
            value for value, _label in Order.PaymentMethod.choices
            if value != Order.PaymentMethod.MIXED
        }
        candidates = []

        for order in orders:
            # Lock the evidence too.  The command is intentionally conservative:
            # one exact tender line, no deleted history, intact live items and
            # header arithmetic, matching branch, and the payment arriving after
            # the stale header are all required.
            payments = list(
                OrderPayment.objects.select_for_update()
                .filter(order=order)
                .order_by('created_at', 'pk')
            )
            if len(payments) != 1 or payments[0].is_deleted:
                continue
            payment = payments[0]
            if payment.method not in concrete_methods:
                continue
            if payment.branch_id != order.branch_id:
                continue
            if payment.amount != order.total_amount or payment.amount < 0:
                continue
            if not order.synced_at or not payment.synced_at:
                continue
            if payment.synced_at <= order.synced_at:
                continue

            items = list(
                OrderItem.objects.select_for_update()
                .filter(order=order, is_deleted=False)
            )
            if not items:
                continue
            item_gross = sum(
                (item.price * item.quantity for item in items), Decimal('0')
            )
            if item_gross != order.subtotal:
                continue
            if order.total_amount != order.subtotal - order.discount_amount:
                continue

            candidates.append((order, payment))

        candidate_total = sum(
            (order.total_amount for order, _payment in candidates), Decimal('0')
        )
        candidate_uuids = sorted(str(order.uuid) for order, _ in candidates)
        fingerprint = hashlib.sha256(
            '\n'.join(candidate_uuids).encode('ascii')
        ).hexdigest()

        self._check_expectations(
            candidates, candidate_total, fingerprint, options,
        )

        self.stdout.write(
            'Candidates: {count}; total: {total}; fingerprint: {fingerprint}'.format(
                count=len(candidates),
                total=f'{candidate_total:.2f}',
                fingerprint=fingerprint,
            )
        )
        for order_uuid in candidate_uuids:
            self.stdout.write(order_uuid)

        if not apply_changes:
            self.stdout.write(self.style.WARNING('Dry-run only; no rows changed.'))
            return

        for order, payment in candidates:
            order.is_paid = True
            order.payment_method = payment.method
            # This is the closest cloud-side proxy available.  The payment was
            # created immediately after the paid header in the till transaction.
            order.paid_at = payment.created_at
            order.save(update_fields=['is_paid', 'payment_method', 'paid_at'])

        self.stdout.write(
            self.style.SUCCESS(
                f'Repaired {len(candidates)} order header(s), '
                f'{candidate_total:.2f} total.'
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
