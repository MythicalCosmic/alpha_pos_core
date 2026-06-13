from django.conf import settings
from django.core.cache import cache


CACHE_PREFIX = 'notif'

# Legacy module-level constants kept for one release as fallbacks; the
# canonical source of truth is now the DB-backed NotificationSettings row
# admins edit from the admin API. Reading at import time meant a freshly-
# deployed instance saw `''` forever — the inbound Telegram bot then
# silently failed every reply because TELEGRAM_BOT_TOKEN was never
# defined in settings.py. `get_bot_token` / `get_chat_ids` below resolve
# at call time against NotificationSettings first.
BOT_TOKEN = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
CHAT_IDS = getattr(settings, 'TELEGRAM_CHAT_IDS', [])
NOTIFICATION_TIMEOUT = getattr(settings, 'NOTIFICATION_TIMEOUT', 10)


def _load_settings():
    try:
        from notifications.models import NotificationSettings
        return NotificationSettings.load()
    except Exception:
        return None


class NotificationConfig:

    TYPES = ['order.new', 'order.ready', 'order.cancelled', 'order.paid',
             'shift.start', 'shift.end', 'shift.switch',
             'hr.contract_expiry', 'hr.probation_end', 'hr.document_expiry']

    @classmethod
    def _key(cls, notification_type):
        return f'{CACHE_PREFIX}:enabled:{notification_type}'

    @classmethod
    def _global_key(cls):
        return f'{CACHE_PREFIX}:enabled:global'

    @classmethod
    def is_enabled(cls, notification_type=None):
        global_enabled = cache.get(cls._global_key())
        if global_enabled is False:
            return False

        if notification_type:
            type_enabled = cache.get(cls._key(notification_type))
            if type_enabled is False:
                return False

        return True

    @classmethod
    def enable(cls, notification_type=None):
        if notification_type:
            cache.set(cls._key(notification_type), True, None)
        else:
            cache.set(cls._global_key(), True, None)

    @classmethod
    def disable(cls, notification_type=None):
        if notification_type:
            cache.set(cls._key(notification_type), False, None)
        else:
            cache.set(cls._global_key(), False, None)

    @classmethod
    def get_status(cls):
        global_enabled = cache.get(cls._global_key())
        status = {
            'global': global_enabled is not False,
            'types': {},
        }
        for t in cls.TYPES:
            val = cache.get(cls._key(t))
            status['types'][t] = val is not False
        return status

    @classmethod
    def get_chat_ids(cls):
        # DB-backed settings take precedence; fall back to the legacy
        # settings.py value so existing deployments don't regress.
        ns = _load_settings()
        if ns and ns.chat_ids:
            return ns.chat_ids
        return CHAT_IDS

    @classmethod
    def get_bot_token(cls):
        ns = _load_settings()
        if ns and ns.bot_token:
            return ns.bot_token
        return BOT_TOKEN
