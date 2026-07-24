"""Requeue dead-lettered sync records so they push again.

A record that fails to push ``SYNC_MAX_QUEUE_ATTEMPTS`` times is *dead-lettered*:
``SyncQueue.get_grouped()`` drops it from the outbound batch so one poison row
can't block the healthy ones (see base/services/sync/queue.py). That's the right
steady state, but it also means a row stuck by a *transient* server-side fault
(e.g. the receiver failing to parse a datetime and rejecting the write) never
retries on its own — it just stays missing on the cloud. That is the mechanism
behind the "shift is missing on the panel" reports: the till holds it, but it's
past the retry cap and no longer sent.

This command resets those rows' attempt counter to 0 while retaining the
original rejection behind a ``[RETRYING]`` marker, so the next push cycle picks
them up without destroying the operator's only diagnosis. Run it *after* the
underlying cause is fixed. Scope to one model with --model, preview with
--dry-run, and add --push to send immediately instead of waiting for the next
sync tick.
"""
from collections import Counter

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Reset dead-lettered sync queue records (attempts >= cap) so they re-push.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--model', dest='model', default=None,
            help='Only requeue this model_name (e.g. shift, order). Default: all.',
        )
        parser.add_argument(
            '--push', action='store_true',
            help='Run a push cycle immediately after requeueing.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be requeued without writing.',
        )

    def handle(self, *args, **opts):
        from base.models import SyncQueueRecord
        from base.services.sync.config import get_sync_max_queue_attempts
        from django.db import transaction
        from django.db.models import Q

        max_attempts = get_sync_max_queue_attempts()
        dead = (
            Q(last_error__startswith='[REJECTED]')
            | Q(last_error__startswith='[BRANCH_SCOPE]')
            | Q(last_error__startswith='[RETRYING]')
        )
        if max_attempts:
            dead |= Q(attempts__gte=max_attempts)
        qs = SyncQueueRecord.objects.filter(dead)
        model = opts.get('model')
        if model:
            qs = qs.filter(model_name=model)

        by_model = Counter(qs.values_list('model_name', flat=True))
        total = sum(by_model.values())
        if not total:
            scope = f" for model '{model}'" if model else ''
            self.stdout.write(f'No dead-lettered records found{scope}.')
            return

        self.stdout.write('Dead-lettered records:')
        for name, n in sorted(by_model.items()):
            self.stdout.write(f'  {name}: {n}')
        self.stdout.write(f'Total: {total}')

        if opts.get('dry_run'):
            self.stdout.write(self.style.WARNING('--dry-run: no changes written.'))
            return

        with transaction.atomic():
            rows = list(qs.select_for_update())
            for row in rows:
                original = (row.last_error or 'dead-lettered record').strip()
                if original.startswith('[RETRYING]'):
                    marker = original.split(' | latest push:', 1)[0]
                else:
                    marker = f'[RETRYING] {original}'
                row.attempts = 0
                row.last_error = marker[:500].rstrip()
            if rows:
                SyncQueueRecord.objects.bulk_update(
                    rows, ['attempts', 'last_error'],
                )
        updated = len(rows)
        self.stdout.write(self.style.SUCCESS(f'Requeued {updated} record(s).'))

        if opts.get('push'):
            from base.services.sync.service import SyncService
            result = SyncService.push()
            ok = result.get('success')
            synced = result.get('synced', result.get('total_synced'))
            self.stdout.write(
                self.style.SUCCESS(f'Push: {result}') if ok
                else self.style.ERROR(f'Push: {result}')
            )
            if synced is not None:
                self.stdout.write(f'Synced this cycle: {synced}')
