import signal
import logging
from time import sleep
from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Manage sync system: push, status, toggle, worker, queue'

    def __init__(self):
        super().__init__()
        self.running = True

    def add_arguments(self, parser):
        parser.add_argument('--status', action='store_true', help='Show sync status')
        parser.add_argument('--push', action='store_true', help='Push pending records')
        parser.add_argument('--full-push', action='store_true', help='Queue all records and push')
        parser.add_argument('--report', action='store_true', help='Show detailed model report')
        parser.add_argument('--worker', action='store_true', help='Start background sync worker')
        parser.add_argument('--interval', type=int, help='Worker push interval in seconds')
        parser.add_argument('--enable', action='store_true', help='Enable sync')
        parser.add_argument('--disable', action='store_true', help='Disable sync')
        parser.add_argument('--on-save', action='store_true', help='Enable SYNC_ON_SAVE (auto-queue on save)')
        parser.add_argument('--off-save', action='store_true', help='Disable SYNC_ON_SAVE')
        parser.add_argument('--queue', action='store_true', help='Show queue contents')
        parser.add_argument('--clear', action='store_true', help='Clear sync queue')
        parser.add_argument('--pull', action='store_true', help='Pull changes from cloud')
        parser.add_argument('--health', action='store_true', help='Check cloud server health')
        parser.add_argument('--config', action='store_true', help='Show sync configuration')

    def handle(self, *args, **options):
        if options['status']:
            self._show_status()
        elif options['push']:
            self._push()
        elif options['full_push']:
            self._full_push()
        elif options['report']:
            self._show_report()
        elif options['worker']:
            self._run_worker(options.get('interval'))
        elif options['enable']:
            self._toggle(True)
        elif options['disable']:
            self._toggle(False)
        elif options['on_save']:
            self._toggle_on_save(True)
        elif options['off_save']:
            self._toggle_on_save(False)
        elif options['pull']:
            self._pull()
        elif options['queue']:
            self._show_queue()
        elif options['clear']:
            self._clear_queue()
        elif options['health']:
            self._check_health()
        elif options['config']:
            self._show_config()
        else:
            self._print_help()

    def _show_status(self):
        from base.services.sync.service import SyncService
        from base.services.sync.cache import safe_get

        status = SyncService.get_status()
        on_save_override = safe_get('sync:config:on_save')
        on_save = on_save_override if on_save_override is not None else getattr(settings, 'SYNC_ON_SAVE', False)

        self.stdout.write('\n  Sync Status')
        self.stdout.write('  ' + '=' * 35)
        self.stdout.write(f'  Enabled: {"YES" if status["enabled"] else "NO"}')
        self.stdout.write(f'  On save: {"YES" if on_save else "NO"}')
        self.stdout.write(f'  Branch: {status["mode"]}')
        self.stdout.write(f'  Online: {"YES" if status["is_online"] else "NO"}')
        self.stdout.write(f'  Last push: {status["last_sync"] or "Never"}')
        self.stdout.write(f'  Last pull: {status["last_pull"] or "Never"}')
        self.stdout.write(f'  Pending: {status["pending_count"]}')
        self.stdout.write(f'  Failed: {status["failed_count"]}')

        if status['last_error']:
            self.stdout.write(f'  Last error: {status["last_error"]}')

        summary = status.get('pending_by_model', {})
        if summary:
            self.stdout.write('\n  Pending by model:')
            for model, count in summary.items():
                self.stdout.write(f'    {model}: {count}')
        self.stdout.write('')

    def _push(self):
        from base.services.sync.service import SyncService
        from base.services.sync.config import SyncConfig, is_local_mode

        if not SyncConfig.is_enabled():
            self.stdout.write(self.style.WARNING('  Sync not enabled. Use --enable first.'))
            return

        if not is_local_mode():
            self.stdout.write(self.style.WARNING('  Push only available in local mode.'))
            return

        self.stdout.write('  Pushing...')
        result = SyncService.push()

        if result.get('success'):
            synced = result.get('synced', 0)
            self.stdout.write(self.style.SUCCESS(f'  Synced: {synced} records'))
        else:
            self.stdout.write(self.style.ERROR(f'  Failed: {result.get("message", "Unknown")}'))
            for err in result.get('errors', []):
                self.stdout.write(self.style.ERROR(f'    {err}'))

    def _full_push(self):
        from base.services.sync.service import SyncService
        from base.services.sync.config import SyncConfig, is_local_mode

        if not SyncConfig.is_enabled():
            self.stdout.write(self.style.WARNING('  Sync not enabled. Use --enable first.'))
            return

        if not is_local_mode():
            self.stdout.write(self.style.WARNING('  Push only available in local mode.'))
            return

        self.stdout.write('  Queuing all records and pushing...')
        result = SyncService.full_push()

        if result.get('success'):
            self.stdout.write(self.style.SUCCESS(f'  Synced: {result.get("synced", 0)} records'))
        else:
            self.stdout.write(self.style.ERROR(f'  Failed: {result.get("message", "Unknown")}'))

    def _pull(self):
        from base.services.sync.service import SyncService
        from base.services.sync.config import SyncConfig, is_local_mode

        if not SyncConfig.is_enabled():
            self.stdout.write(self.style.WARNING('  Sync not enabled. Use --enable first.'))
            return

        if not is_local_mode():
            self.stdout.write(self.style.WARNING('  Pull only available in local mode.'))
            return

        self.stdout.write('  Pulling from cloud...')
        result = SyncService.pull_from_cloud()

        if result.get('success'):
            created = result.get('created', 0)
            updated = result.get('updated', 0)
            self.stdout.write(self.style.SUCCESS(f'  Created: {created}, Updated: {updated}'))
        elif result.get('offline'):
            self.stdout.write(self.style.WARNING('  Cloud server unreachable.'))
        else:
            self.stdout.write(self.style.ERROR(f'  Failed: {result.get("message", "Unknown")}'))

    def _show_report(self):
        from base.services.sync.service import SyncService

        report = SyncService.status_report()

        self.stdout.write('\n  Sync Report')
        self.stdout.write('  ' + '=' * 45)
        self.stdout.write(f'  Branch: {report["branch_id"]}')
        self.stdout.write(f'  Last push: {report["last_push"] or "Never"}')
        self.stdout.write(f'  Last pull: {report["last_pull"] or "Never"}')

        for name, info in report.get('models', {}).items():
            self.stdout.write(f'\n  {name}:')
            self.stdout.write(f'    Total: {info["total"]}')
            self.stdout.write(f'    Synced: {info["synced"]}')
            self.stdout.write(f'    Unsynced: {info["unsynced"]}')
            self.stdout.write(f'    Last synced: {info["last_synced"] or "Never"}')
        self.stdout.write('')

    def _run_worker(self, custom_interval=None):
        from base.services.sync.config import (
            SyncConfig, is_local_mode, get_sync_interval, get_pull_enabled,
        )

        if not SyncConfig.is_enabled():
            self.stdout.write(self.style.WARNING('  Sync not enabled. Use --enable first.'))
            return

        if not is_local_mode():
            self.stdout.write(self.style.WARNING('  Worker only available in local mode.'))
            return

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        interval = custom_interval or get_sync_interval()
        self.stdout.write(self.style.SUCCESS('  Sync worker started (bidirectional)'))
        self.stdout.write(f'  Interval: {interval}s')
        self.stdout.write(f'  Pull: {"ON" if get_pull_enabled() else "OFF"}')
        self.stdout.write('  Press Ctrl+C to stop.\n')

        from base.services.sync.service import SyncService
        from base.services.sync.config import get_sync_retry_interval
        from base.notifications.helpers import uzb_now

        while self.running:
            try:
                ts = uzb_now().strftime('%H:%M:%S')

                push_result = SyncService.push()

                if push_result.get('offline'):
                    self.stdout.write(self.style.WARNING(f'  [{ts}] Offline'))
                    sleep(get_sync_retry_interval())
                    continue

                pushed = push_result.get('synced', 0)
                if pushed > 0:
                    self.stdout.write(self.style.SUCCESS(f'  [{ts}] Pushed {pushed}'))

                if get_pull_enabled():
                    pull_result = SyncService.pull_from_cloud()
                    if pull_result.get('success'):
                        created = pull_result.get('created', 0)
                        updated = pull_result.get('updated', 0)
                        if created > 0 or updated > 0:
                            self.stdout.write(self.style.SUCCESS(
                                f'  [{ts}] Pulled +{created} ~{updated}'
                            ))

                sleep(interval)

            except Exception as e:
                logger.error(f'Worker error: {e}')
                self.stdout.write(self.style.ERROR(f'  Error: {e}'))
                sleep(get_sync_retry_interval())

        self.stdout.write(self.style.SUCCESS('\n  Worker stopped.'))

    def _toggle(self, enable):
        from base.services.sync.config import SyncConfig

        if enable:
            SyncConfig.enable()
            self.stdout.write(self.style.SUCCESS('  Sync enabled'))
        else:
            SyncConfig.disable()
            self.stdout.write(self.style.WARNING('  Sync disabled'))

    def _toggle_on_save(self, enable):
        from base.services.sync.cache import safe_set

        safe_set('sync:config:on_save', enable, None)
        if enable:
            self.stdout.write(self.style.SUCCESS('  SYNC_ON_SAVE enabled (auto-queue on model save)'))
        else:
            self.stdout.write(self.style.WARNING('  SYNC_ON_SAVE disabled'))

    def _show_queue(self):
        from base.services.sync.queue import SyncQueue

        records = SyncQueue.get_all()
        count = len(records)
        self.stdout.write(f'\n  Queue: {count} records')

        if count == 0:
            self.stdout.write('')
            return

        summary = SyncQueue.get_summary()
        for model, cnt in summary.items():
            self.stdout.write(f'    {model}: {cnt}')

        self.stdout.write('\n  Recent (max 20):')
        for r in records[:20]:
            attempts = r.get('attempts', 0)
            err = f' [{r["last_error"][:40]}]' if r.get('last_error') else ''
            self.stdout.write(f'    {r["model_name"]}:{r["uuid"]} (x{attempts}){err}')
        self.stdout.write('')

    def _clear_queue(self):
        from base.services.sync.queue import SyncQueue

        count = SyncQueue.clear()
        self.stdout.write(self.style.SUCCESS(
            f'  Cleared {count} rebuildable queued records; '
            'hard-delete tombstones preserved.'
        ))

    def _check_health(self):
        from base.services.sync.transport import check_health
        from base.services.sync.config import get_cloud_url

        url = get_cloud_url()
        self.stdout.write(f'\n  Cloud URL: {url or "NOT SET"}')

        if not url:
            self.stdout.write(self.style.ERROR('  Configure CLOUD_SYNC_URL in settings.'))
            self.stdout.write('')
            return

        if check_health():
            self.stdout.write(self.style.SUCCESS('  Cloud server: reachable'))
        else:
            self.stdout.write(self.style.ERROR('  Cloud server: unreachable'))
        self.stdout.write('')

    def _show_config(self):
        from base.services.sync.config import SyncConfig
        from base.services.sync.cache import safe_get

        config = SyncConfig.get_status()
        on_save_override = safe_get('sync:config:on_save')
        on_save = on_save_override if on_save_override is not None else getattr(settings, 'SYNC_ON_SAVE', False)

        self.stdout.write('\n  Sync Configuration')
        self.stdout.write('  ' + '=' * 35)
        self.stdout.write(f'  Enabled: {"YES" if config["enabled"] else "NO"}')
        self.stdout.write(f'  On save: {"YES" if on_save else "NO"}')
        self.stdout.write(f'  Mode: {config["mode"]}')
        self.stdout.write(f'  Branch: {config["branch_id"]}')
        self.stdout.write(f'  Cloud URL: {config["cloud_url"] or "NOT SET"}')
        self.stdout.write(f'  Interval: {config["interval"]}s')
        self.stdout.write(f'  Batch size: {config["batch_size"]}')
        self.stdout.write(f'  Max retries: {config["max_retries"]}')
        self.stdout.write(f'  Pull enabled: {"YES" if config["pull_enabled"] else "NO"}')
        self.stdout.write('')

    def _print_help(self):
        self.stdout.write('\n  Usage:')
        self.stdout.write('    --status       Show sync status')
        self.stdout.write('    --push         Push pending records')
        self.stdout.write('    --full-push    Queue all records and push')
        self.stdout.write('    --pull         Pull changes from cloud')
        self.stdout.write('    --report       Detailed model sync report')
        self.stdout.write('    --worker       Start background sync worker')
        self.stdout.write('    --interval N   Worker push interval in seconds')
        self.stdout.write('    --enable       Enable sync')
        self.stdout.write('    --disable      Disable sync')
        self.stdout.write('    --on-save      Enable auto-queue on model save')
        self.stdout.write('    --off-save     Disable auto-queue on model save')
        self.stdout.write('    --queue        Show queue contents')
        self.stdout.write('    --clear        Clear sync queue')
        self.stdout.write('    --health       Check cloud server health')
        self.stdout.write('    --config       Show configuration')
        self.stdout.write('')

    def _signal_handler(self, signum, frame):
        self.stdout.write('\n  Stopping...')
        self.running = False
