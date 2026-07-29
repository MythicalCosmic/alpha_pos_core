"""Repair ownership that hides cloud-managed rows from branch pull feeds.

Cloud mode is required. The default is a preview; ``--apply`` locks and
re-stamps the selected rows to the cloud node's ``BRANCH_ID``.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from base.models import User, Category, Product

MODELS = {'user': User, 'category': Category, 'product': Product}


class Command(BaseCommand):
    help = (
        "Repair cloud-managed records' branch ownership. "
        'Dry-run unless --apply is supplied.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'models',
            nargs='*',
            help='user category product (default: user)',
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            '--apply',
            action='store_true',
            help='Apply the reviewed ownership changes.',
        )
        mode.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview only (the default; retained for compatibility).',
        )

    def handle(self, *args, **options):
        deployment_mode = str(
            getattr(settings, 'DEPLOYMENT_MODE', '') or ''
        ).strip().lower()
        if deployment_mode != 'cloud':
            raise CommandError(
                'repair_cloud_record_ownership is restricted to cloud mode'
            )

        target = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
        if not target:
            raise CommandError('Cloud BRANCH_ID must not be empty')
        if len(target) > 50:
            raise CommandError('Cloud BRANCH_ID must be at most 50 characters')

        apply_changes = bool(options['apply'])
        names = options['models'] or ['user']
        self.stdout.write(f'Target branch_id = {target!r}'
                          + ('' if apply_changes else '  (DRY RUN)'))
        total = 0
        with transaction.atomic():
            for name in names:
                model = MODELS.get(name.lower())
                if not model:
                    raise CommandError(f'Unknown model: {name}')

                candidates = model.objects.filter(
                    Q(branch_id__isnull=True) | ~Q(branch_id=target)
                ).order_by('pk')
                if apply_changes:
                    candidates = candidates.select_for_update()
                rows = list(candidates)

                if apply_changes:
                    for obj in rows:
                        obj.branch_id = target
                        obj.save(
                            update_fields=[
                                'branch_id', 'synced_at', 'sync_version',
                            ]
                        )

                count = len(rows)
                action = 're-stamped' if apply_changes else 'would re-stamp'
                self.stdout.write(f'  {name}: {action} {count}')
                total += count

        verb = 'Repaired' if apply_changes else 'Would repair'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {total} record(s) to {target!r}.'
        ))
