"""Canary for the tender-attribution invariant.

Reports paid, non-cancelled orders whose tender cannot be determined from payment
lines: a non-cash rolled-up method with ZERO OrderPayment rows. On a healthy system
this is 0.

It is the ONLY detector for the sync dead-letter hole: `base/services/sync/config.py`
pushes `order` before `orderpayment`, and `queue.py` drops a record permanently once
it hits max_attempts. So an Order can land on the cloud while its payment lines never
do — and because cash is defined as the residual (total - noncash), the sale would
otherwise be silently reported as 100% cash, forever, passing every sum-to-revenue check.

    python manage.py check_tender_attribution [--days 30] [--fail]
"""
from django.core.management.base import BaseCommand
from django.db.models import Sum


class Command(BaseCommand):
    help = 'Report paid orders whose tender cannot be attributed from payment lines.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=0,
                            help='Only look at the last N days (0 = all time).')
        parser.add_argument('--fail', action='store_true',
                            help='Exit non-zero when anything is unattributable (for cron/CI).')

    def handle(self, *args, **opts):
        from datetime import timedelta
        from django.utils import timezone
        from base.models import Order
        from base.services.tender import unattributed_orders, breakdown_for_orders

        qs = Order.objects.filter(is_deleted=False, is_paid=True).exclude(status='CANCELED')
        if opts['days']:
            qs = qs.filter(paid_at__gte=timezone.now() - timedelta(days=opts['days']))

        flagged = unattributed_orders(qs)
        n = flagged.count()
        amount = flagged.aggregate(s=Sum('total_amount'))['s'] or 0

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
                f'\nUNATTRIBUTABLE: {n} paid order(s) worth {amount} have a non-cash '
                f'payment_method and NO OrderPayment rows.'))
            for o in flagged.order_by('-paid_at')[:20]:
                self.stdout.write(f'  order {o.id}  {o.payment_method}  {o.total_amount}  paid_at={o.paid_at}')
        else:
            self.stdout.write(self.style.SUCCESS('\nOK: every paid order has attributable tender.'))

        if opts['fail'] and (n or not ok):
            raise SystemExit(1)
