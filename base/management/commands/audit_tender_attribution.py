"""Report paid orders with missing or incomplete tender evidence.

This catches dead-lettered payment children that would otherwise make residual
cash reporting misclassify an order.
"""
from django.core.management.base import BaseCommand
from django.db.models import Sum


class Command(BaseCommand):
    help = 'Audit paid orders whose tender cannot be attributed from payment lines.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=0,
                            help='Only look at the last N days (0 = all time).')
        parser.add_argument('--fail', action='store_true',
                            help='Exit non-zero when anything is unattributable (for cron/CI).')

    def handle(self, *args, **opts):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order
        from base.services.tender import tender_integrity_issues, breakdown_for_orders

        # A refunded/cancelled paid order remains an immutable sale event; its
        # separate OrderRefund cannot make missing original tender evidence OK.
        qs = Order.objects.filter(is_deleted=False, is_paid=True)
        if opts['days']:
            qs = qs.filter(paid_at__gte=timezone.now() - timedelta(days=opts['days']))

        issues = tender_integrity_issues(qs)
        n = len(issues)
        amount = sum((issue['amount'] for issue in issues), 0)

        split, _ = breakdown_for_orders(qs)
        revenue = qs.aggregate(s=Sum('total_amount'))['s'] or 0
        total = split['cash'] + split['card'] + split['payme'] + split['unknown']

        self.stdout.write(f'orders checked      : {qs.count()}')
        self.stdout.write(f'revenue             : {revenue}')
        self.stdout.write(f"  cash              : {split['cash']}")
        self.stdout.write(f"  card              : {split['card']}")
        self.stdout.write(f"  payme             : {split['payme']}")
        self.stdout.write(f"  unknown           : {split['unknown']}")
        ok = (total == revenue)
        self.stdout.write(f'buckets sum to revenue: {ok}')

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

        if opts['fail'] and (n or not ok):
            raise SystemExit(1)
