"""Process durable staff-Telegram delivery/edit retries.

The normal send path is an in-process worker for low latency. Transport failures
are persisted in ``notifications.services.QueueService`` (Redis in production);
this single server-side command drains that queue so a timeout or a temporarily
missing order message id does not leave the lifecycle card stale forever.
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from notifications.services.queue_service import QueueService


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Retry pending staff Telegram notifications and in-place edits'

    def __init__(self):
        super().__init__()
        self.running = True

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=15,
            help='Seconds between retry passes (default: 15)',
        )
        parser.add_argument(
            '--once',
            action='store_true',
            help='Process the durable queue once, then exit',
        )

    def handle(self, *args, **options):
        interval = max(1, int(options['interval']))
        if options['once']:
            self._process_once()
            return

        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)
        self.stdout.write(self.style.SUCCESS(
            f'Staff Telegram retry worker started (interval={interval}s)',
        ))

        while self.running:
            self._process_once()
            # Sleep in one-second slices so container shutdown is prompt.
            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

        self.stdout.write(self.style.SUCCESS('Staff Telegram retry worker stopped'))

    def _process_once(self):
        try:
            if QueueService.count():
                sent, failed = QueueService.process()
                if sent or failed:
                    dead = QueueService.dead_letter_count()
                    self.stdout.write(
                        'Staff Telegram retry pass: '
                        f'sent={sent}, pending={failed}, dead={dead}',
                    )
        except Exception:
            logger.exception('staff Telegram retry pass failed')
        finally:
            close_old_connections()

    def _stop(self, signum, frame):
        self.running = False
