from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from django.core.cache import cache


class Command(BaseCommand):
    help = 'Check and send HR reminders (contracts, probation, documents)'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show what would be sent without sending')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        today = timezone.now().date()
        from hr.models import EmployeeContract, EmployeeDocument
        from notifications.handlers.hr import HRNotification

        # Check contracts expiring in 30, 14, 7, 1 days
        for days in [30, 14, 7, 1]:
            target = today + timedelta(days=days)
            contracts = EmployeeContract.objects.filter(
                status='ACTIVE', end_date=target, is_deleted=False
            ).select_related('employee__user')
            for c in contracts:
                key = f'hr:reminder:contract:{c.id}:{days}'
                if cache.get(key):
                    continue
                if dry_run:
                    self.stdout.write(f'  Would notify: Contract {c.contract_number} expires in {days} days')
                else:
                    HRNotification.on_contract_expiring(c, days)
                    cache.set(key, True, 86400)
                    self.stdout.write(f'  Sent: Contract {c.contract_number} expires in {days} days')

        # Check probation ending in 7, 1 days
        for days in [7, 1]:
            target = today + timedelta(days=days)
            contracts = EmployeeContract.objects.filter(
                status='ACTIVE', probation_end_date=target, is_deleted=False
            ).select_related('employee__user')
            for c in contracts:
                key = f'hr:reminder:probation:{c.id}:{days}'
                if cache.get(key):
                    continue
                if dry_run:
                    self.stdout.write(f'  Would notify: Probation for {c.employee} ends in {days} days')
                else:
                    HRNotification.on_probation_ending(c, days)
                    cache.set(key, True, 86400)
                    self.stdout.write(f'  Sent: Probation for {c.employee} ends in {days} days')

        # Check documents expiring in 30, 7 days
        for days in [30, 7]:
            target = today + timedelta(days=days)
            docs = EmployeeDocument.objects.filter(
                expiry_date=target, is_deleted=False
            ).select_related('employee__user')
            for d in docs:
                key = f'hr:reminder:doc:{d.id}:{days}'
                if cache.get(key):
                    continue
                if dry_run:
                    self.stdout.write(f'  Would notify: Document {d.title} for {d.employee} expires in {days} days')
                else:
                    HRNotification.on_document_expiring(d, days)
                    cache.set(key, True, 86400)
                    self.stdout.write(f'  Sent: Document {d.title} expires in {days} days')

        self.stdout.write(self.style.SUCCESS('HR reminders check complete'))
