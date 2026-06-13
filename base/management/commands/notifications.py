import signal
import logging
from time import sleep
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Manage notification system: process pending, toggle on/off, check status'

    def __init__(self):
        super().__init__()
        self.running = True

    def add_arguments(self, parser):
        parser.add_argument('--process', action='store_true', help='Process pending notifications once')
        parser.add_argument('--daemon', action='store_true', help='Run pending processor as daemon')
        parser.add_argument('--interval', type=int, default=120, help='Daemon check interval (default: 120s)')
        parser.add_argument('--status', action='store_true', help='Show notification system status')
        parser.add_argument('--enable', type=str, nargs='?', const='global', help='Enable notifications (global or type)')
        parser.add_argument('--disable', type=str, nargs='?', const='global', help='Disable notifications (global or type)')
        parser.add_argument('--check', action='store_true', help='Check Telegram connection')
        parser.add_argument('--test', action='store_true', help='Send test message')
        parser.add_argument('--clear', action='store_true', help='Clear pending queue')
        parser.add_argument('--shift', action='store_true', help='Show current shift info')

    def handle(self, *args, **options):
        if options['process']:
            self._process_once()
        elif options['daemon']:
            self._run_daemon(options['interval'])
        elif options['status']:
            self._show_status()
        elif options['enable']:
            self._toggle(options['enable'], True)
        elif options['disable']:
            self._toggle(options['disable'], False)
        elif options['check']:
            self._check_connection()
        elif options['test']:
            self._send_test()
        elif options['clear']:
            self._clear_queue()
        elif options['shift']:
            self._show_shift()
        else:
            self._print_help()

    def _process_once(self):
        from base.notifications.queue import NotificationQueue
        from base.notifications.telegram import TelegramAPI

        count = NotificationQueue.count()
        if count == 0:
            self.stdout.write('No pending notifications.')
            return

        self.stdout.write(f'Found {count} pending notifications.')

        if not TelegramAPI.is_online():
            self.stdout.write(self.style.WARNING('Telegram offline.'))
            return

        sent, failed = NotificationQueue.process()
        self.stdout.write(self.style.SUCCESS(f'Sent: {sent}, Failed: {failed}'))

    def _run_daemon(self, interval):
        from base.notifications.queue import NotificationQueue
        from base.notifications.telegram import TelegramAPI
        from base.notifications.helpers import uzb_now

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self.stdout.write(self.style.SUCCESS('Notification processor started (daemon)'))
        self.stdout.write(f'Interval: {interval}s')
        self.stdout.write(f'Time: {uzb_now().strftime("%Y-%m-%d %H:%M:%S")}')
        self.stdout.write('Press Ctrl+C to stop.\n')

        while self.running:
            try:
                count = NotificationQueue.count()
                if count > 0:
                    ts = uzb_now().strftime('%H:%M:%S')
                    self.stdout.write(f'[{ts}] {count} pending')

                    if TelegramAPI.is_online():
                        sent, failed = NotificationQueue.process()
                        if sent > 0:
                            self.stdout.write(self.style.SUCCESS(f'  Sent {sent}'))
                        if failed > 0:
                            self.stdout.write(self.style.WARNING(f'  Failed {failed}'))
                    else:
                        self.stdout.write(self.style.WARNING('  Telegram offline'))

                sleep(interval)
            except Exception as e:
                logger.error(f'Processor error: {e}')
                self.stdout.write(self.style.ERROR(f'Error: {e}'))
                sleep(interval)

        self.stdout.write(self.style.SUCCESS('\nProcessor stopped.'))

    def _show_status(self):
        from base.notifications.config import NotificationConfig
        from base.notifications.queue import NotificationQueue
        from base.notifications.shift import ShiftSession

        status = NotificationConfig.get_status()

        self.stdout.write('\n  Notification System Status')
        self.stdout.write('  ' + '=' * 35)
        self.stdout.write(f'  Global: {"ON" if status["global"] else "OFF"}')
        self.stdout.write('')

        for t, enabled in status['types'].items():
            state = 'ON' if enabled else 'OFF'
            self.stdout.write(f'  {t}: {state}')

        self.stdout.write(f'\n  Pending queue: {NotificationQueue.count()}')

        session = ShiftSession.get_info()
        if session:
            self.stdout.write(f'  Active shift: {session["user_name"]} ({session["duration"]})')
        else:
            self.stdout.write('  Active shift: None')
        self.stdout.write('')

    def _toggle(self, target, enable):
        from base.notifications.config import NotificationConfig

        if target == 'global':
            if enable:
                NotificationConfig.enable()
                self.stdout.write(self.style.SUCCESS('  Notifications enabled globally'))
            else:
                NotificationConfig.disable()
                self.stdout.write(self.style.WARNING('  Notifications disabled globally'))
        else:
            if target not in NotificationConfig.TYPES:
                self.stdout.write(self.style.ERROR(f'  Unknown type: {target}'))
                self.stdout.write(f'  Available: {", ".join(NotificationConfig.TYPES)}')
                return
            if enable:
                NotificationConfig.enable(target)
                self.stdout.write(self.style.SUCCESS(f'  {target} enabled'))
            else:
                NotificationConfig.disable(target)
                self.stdout.write(self.style.WARNING(f'  {target} disabled'))

    def _check_connection(self):
        from base.notifications.telegram import TelegramAPI
        from base.notifications.config import NotificationConfig

        token = NotificationConfig.get_bot_token()
        chat_ids = NotificationConfig.get_chat_ids()

        self.stdout.write(f'\n  Bot token: {"configured" if token else "MISSING"}')
        self.stdout.write(f'  Chat IDs: {chat_ids if chat_ids else "MISSING"}')

        if TelegramAPI.is_online():
            self.stdout.write(self.style.SUCCESS('  Telegram API: reachable'))
        else:
            self.stdout.write(self.style.ERROR('  Telegram API: unreachable'))
        self.stdout.write('')

    def _send_test(self):
        from base.notifications.telegram import TelegramAPI
        from base.notifications.helpers import uzb_now

        now = uzb_now()
        text = (
            f'<b>TEST — Alpha POS</b>\n\n'
            f'Agar siz buni ko\'rsangiz, integratsiya ishlayapti.\n'
            f'Vaqt: {now.strftime("%Y-%m-%d %H:%M:%S")}'
        )

        ok, error = TelegramAPI.send_message(text)
        if ok:
            self.stdout.write(self.style.SUCCESS('  Test message sent.'))
        else:
            self.stdout.write(self.style.ERROR(f'  Failed: {error}'))

    def _clear_queue(self):
        from base.notifications.queue import NotificationQueue
        count = NotificationQueue.count()
        NotificationQueue.clear()
        self.stdout.write(self.style.SUCCESS(f'  Cleared {count} pending notifications.'))

    def _show_shift(self):
        from base.notifications.shift import ShiftSession

        info = ShiftSession.get_info()
        if info:
            self.stdout.write(f'\n  Active shift:')
            self.stdout.write(f'    Cashier: {info["user_name"]}')
            self.stdout.write(f'    User ID: {info["user_id"]}')
            self.stdout.write(f'    Login: {info["login_time"]}')
            self.stdout.write(f'    Duration: {info["duration"]}')
        else:
            self.stdout.write('\n  No active shift')
        self.stdout.write('')

    def _print_help(self):
        self.stdout.write('\n  Usage:')
        self.stdout.write('    --process          Process pending notifications')
        self.stdout.write('    --daemon           Run as daemon')
        self.stdout.write('    --status           Show system status')
        self.stdout.write('    --enable [type]    Enable notifications')
        self.stdout.write('    --disable [type]   Disable notifications')
        self.stdout.write('    --check            Check Telegram connection')
        self.stdout.write('    --test             Send test message')
        self.stdout.write('    --clear            Clear pending queue')
        self.stdout.write('    --shift            Show current shift')
        self.stdout.write('')

    def _signal_handler(self, signum, frame):
        self.stdout.write('\nStopping...')
        self.running = False
