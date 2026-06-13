"""Retry FAILED fiscal receipts (serve-now queue drain).

    python manage.py fiscalize_retry

Run on a schedule (cron / the desktop control panel button) so receipts that
failed during an internet outage get reported once connectivity returns.
"""
from django.core.management.base import BaseCommand

from fiscalization.services import FiscalizationService


class Command(BaseCommand):
    help = 'Retry failed fiscal receipts.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **opts):
        res = FiscalizationService.retry_failed(limit=opts['limit'])
        self.stdout.write(self.style.SUCCESS(
            f"retried={res.get('retried', 0)} confirmed={res.get('confirmed', 0)} "
            f"still_failing={res.get('still_failing', 0)}"
        ))
