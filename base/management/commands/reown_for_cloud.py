"""Re-stamp cloud-managed records' branch_id to THIS node's BRANCH_ID.

The /changes pull feed excludes records whose branch_id == the requesting
branch (anti-echo). So a cloud-managed entity (user, catalog) that ended up
tagged with a branch's id (e.g. created on/pushed from a till) will NOT be sent
back down to that branch when edited on the cloud — its updates appear stuck.

Run this ON THE CLOUD (BRANCH_ID=cloud) to claim those records for the cloud so
their edits propagate down to every branch. Saving bumps sync_version + synced_at
so the records re-enter the /changes feed.

    python manage.py reown_for_cloud            # users (default)
    python manage.py reown_for_cloud user category product
    python manage.py reown_for_cloud --dry-run
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from base.models import User, Category, Product

MODELS = {'user': User, 'category': Category, 'product': Product}


class Command(BaseCommand):
    help = "Re-stamp cloud-managed records' branch_id to this node's BRANCH_ID."

    def add_arguments(self, parser):
        parser.add_argument('models', nargs='*', default=['user'],
                            help='user category product (default: user)')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        target = getattr(settings, 'BRANCH_ID', '') or ''
        dry = options['dry_run']
        names = options['models'] or ['user']
        self.stdout.write(f'Target branch_id = {target!r}'
                          + ('  (DRY RUN)' if dry else ''))
        total = 0
        for name in names:
            model = MODELS.get(name.lower())
            if not model:
                self.stdout.write(self.style.WARNING(f'  unknown model: {name}'))
                continue
            n = 0
            for obj in model.objects.all():
                if (obj.branch_id or '') != target:
                    n += 1
                    if not dry:
                        obj.branch_id = target
                        obj.save(update_fields=['branch_id', 'synced_at', 'sync_version'])
            self.stdout.write(f'  {name}: {"would re-stamp" if dry else "re-stamped"} {n}')
            total += n
        self.stdout.write(self.style.SUCCESS(
            f'{"Would re-own" if dry else "Re-owned"} {total} record(s) to {target!r}.'))
