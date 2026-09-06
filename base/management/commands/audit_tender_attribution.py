"""Report paid orders with missing or incomplete tender evidence.

This catches dead-lettered payment children that would otherwise make residual
cash reporting misclassify an order.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q, Sum


class Command(BaseCommand):
    help = 'Audit paid orders whose tender cannot be attributed from payment lines.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=0,
                            help='Only look at the last N days (0 = all time).')
        parser.add_argument('--fail', action='store_true',
                            help='Exit non-zero when anything is unattributable (for cron/CI).')
        parser.add_argument('--branch', help='Limit the audit to one branch_id.')
        parser.add_argument('--json', action='store_true', dest='as_json',
                            help='Print machine-readable evidence without customer details.')

    def handle(self, *args, **opts):
        if opts['days'] < 0:
            raise CommandError('--days must be non-negative')
        # A live checkout must not land between the header and child queries.
        # Standalone PostgreSQL runs use one read-only snapshot and take no
        # row locks. Nested callers retain their existing transaction settings.
        nested = connection.in_atomic_block
        with transaction.atomic():
            if connection.vendor == 'postgresql' and not nested:
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'
                    )
            result = self._audit(opts)

        if opts.get('as_json'):
            self.stdout.write(json.dumps(result, default=str, indent=2))
        else:
            self._print_audit(result)
        if opts['fail'] and not result['internally_consistent']:
            raise SystemExit(1)

    def _audit(self, opts):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import ExternalOrderPayment, Order, OrderPayment
        from base.services.tender import tender_integrity_issues, breakdown_for_orders

        orders = Order.objects.filter(is_deleted=False)
        if opts.get('branch'):
            orders = orders.filter(branch_id=opts['branch'])
        payments = OrderPayment.objects.filter(
            order_id=OuterRef('pk'), is_deleted=False,
        )
        cutoff = None
        if opts['days']:
            cutoff = timezone.now() - timedelta(days=opts['days'])
            orders = orders.alias(
                _recent_till_payment=Exists(payments.filter(created_at__gte=cutoff)),
                _recent_external_payment=Exists(ExternalOrderPayment.objects.filter(
                    order_id=OuterRef('pk'), is_deleted=False,
                    occurred_at__gte=cutoff,
                )),
            ).filter(
                Q(paid_at__gte=cutoff)
                | Q(paid_at__isnull=True, created_at__gte=cutoff)
                | Q(_recent_till_payment=True)
                | Q(_recent_external_payment=True)
            )

        # A lost paid header used to vanish from this audit altogether. Till
        # evidence on an unpaid order needs review; courier-only partial
        # collections are legitimate and do not by themselves prove a defect.
        header_issues = []
        fields = ('id', 'uuid', 'total_amount', 'payment_method', 'paid_at')
        suspect_sets = (
            ('unpaid order has till payment evidence',
             orders.filter(is_paid=False).alias(_has_till=Exists(payments))
             .filter(_has_till=True)),
            ('paid order has no payment timestamp',
             orders.filter(is_paid=True, paid_at__isnull=True)),
            ('paid order has a negative total',
             orders.filter(is_paid=True, total_amount__lt=0)),
        )
        for reason, suspects in suspect_sets:
            for row in suspects.order_by('pk').values(*fields):
                header_issues.append({
                    'order_id': row['id'], 'order_uuid': str(row['uuid']),
                    'amount': row['total_amount'],
                    'payment_method': row['payment_method'],
                    'paid_at': row['paid_at'], 'reason': reason,
                })

        # A refunded/cancelled paid order remains an immutable sale event; its
        # separate OrderRefund cannot make missing original tender evidence OK.
        qs = orders.filter(is_paid=True)
        issues = tender_integrity_issues(qs)
        split, _ = breakdown_for_orders(qs)
        revenue = qs.aggregate(s=Sum('total_amount'))['s'] or 0
        total = split['cash'] + split['card'] + split['payme'] + split['unknown']
        return {
            'branch_id': opts.get('branch'),
            'since': cutoff,
            'orders_checked': orders.count(),
            'paid_orders_checked': qs.count(),
            'revenue': revenue,
            'payment_breakdown': split,
            'buckets_sum_to_revenue': total == revenue,
            'header_issues': header_issues,
            'tender_issues': issues,
            'internally_consistent': not (issues or header_issues) and total == revenue,
            'physical_reconciliation_proven': False,
            'limitations': [
                'Cash retained is derived from the order total; raw CASH includes change.',
                'This checks stored evidence, not cash counts or acquirer statements.',
                'The --days interval follows payment evidence, not a shift or business day.',
                'No historical records are changed; flagged orders require review.',
            ],
        }

    def _print_audit(self, result):
        split = result['payment_breakdown']
        issues = result['tender_issues']
        n = len(issues)
        amount = sum((issue['amount'] for issue in issues), 0)

        self.stdout.write(f"orders checked      : {result['orders_checked']}")
        self.stdout.write(f"paid orders checked : {result['paid_orders_checked']}")
        self.stdout.write(f"revenue             : {result['revenue']}")
        self.stdout.write(f"  cash              : {split['cash']}")
        self.stdout.write(f"  card              : {split['card']}")
        self.stdout.write(f"  payme             : {split['payme']}")
        self.stdout.write(f"  unknown           : {split['unknown']}")
        self.stdout.write(f"buckets sum to revenue: {result['buckets_sum_to_revenue']}")

        if n:
            self.stdout.write(self.style.ERROR(
                f'\nUNATTRIBUTABLE: {n} paid order(s) worth {amount} have '
                f'missing or incomplete payment evidence.'))
            for issue in issues[:20]:
                self.stdout.write(
                    f"  order {issue['order_id']}  {issue['payment_method']}  "
                    f"{issue['amount']}  {issue['reason']}"
                )
        else:
            self.stdout.write(self.style.SUCCESS('\nOK: every paid order has attributable tender.'))

        if result['header_issues']:
            self.stdout.write(self.style.ERROR(
                f"\nHEADER ISSUES: {len(result['header_issues'])} finding(s)."
            ))
            for issue in result['header_issues'][:20]:
                self.stdout.write(
                    f"  order {issue['order_id']} uuid={issue['order_uuid']}  "
                    f"{issue['amount']}  {issue['reason']}"
                )
        self.stdout.write(
            '\nRead-only internal check; this does not prove a match to physical '
            'cash or card/provider statements.'
        )
