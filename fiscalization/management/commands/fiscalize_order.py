"""Manually fiscalize one order and print the fiscal sign + QR.

    python manage.py fiscalize_order 42
    python manage.py fiscalize_order 42 --refund

Useful for the install smoke test: in mock mode it prints a believable receipt
with no network; once live credentials are set it produces a real fiscal sign
you can verify on ofd.soliq.uz.
"""
from django.core.management.base import BaseCommand, CommandError

from fiscalization.config import FiscalConfig
from fiscalization.models import FiscalReceipt
from fiscalization.services import FiscalizationService


class Command(BaseCommand):
    help = 'Fiscalize a single order through the configured provider.'

    def add_arguments(self, parser):
        parser.add_argument('order_id', type=int)
        parser.add_argument('--refund', action='store_true', help='Fiscalize as a REFUND.')

    def handle(self, *args, **opts):
        self.stdout.write(f'Fiscalization mode: {FiscalConfig.get_mode()} '
                          f'(provider={FiscalConfig.get_provider_name()})')
        if not FiscalConfig.is_enabled():
            raise CommandError('Fiscalization is OFF. Set FISCALIZATION_MODE=mock to test.')

        rtype = (FiscalReceipt.ReceiptType.REFUND if opts['refund']
                 else FiscalReceipt.ReceiptType.SALE)
        result, status = FiscalizationService.fiscalize_order(opts['order_id'], rtype)
        if result.get('success'):
            data = result.get('data', {})
            self.stdout.write(self.style.SUCCESS('Fiscalized:'))
            self.stdout.write(f"  fiscal sign : {data.get('fiscal_sign')}")
            self.stdout.write(f"  number      : {data.get('fiscal_number')}")
            self.stdout.write(f"  QR          : {data.get('qr_url')}")
        else:
            raise CommandError(f"Fiscalization failed: {result.get('message')}")
