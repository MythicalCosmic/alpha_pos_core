import json
import logging
from django.core.cache import cache
from base.notifications.telegram import TelegramAPI

logger = logging.getLogger(__name__)

# Legacy queue from base/notifications/. The newer notifications/ app uses
# 'notif:pending' — using a distinct key here prevents the two systems from
# trampling each other's queue while the legacy emitters are phased out.
QUEUE_KEY = 'notif:pending:legacy'
QUEUE_TTL = 86400


class NotificationQueue:

    @classmethod
    def add(cls, message, notification_type):
        queue = cls._get()
        queue.append({
            'message': message,
            'type': notification_type,
        })
        cls._set(queue)
        logger.info(f'Queued notification: {notification_type}')

    @classmethod
    def count(cls):
        return len(cls._get())

    @classmethod
    def process(cls):
        if not TelegramAPI.is_online():
            return 0, 0

        queue = cls._get()
        if not queue:
            return 0, 0

        sent = 0
        failed = []

        for item in queue:
            ok, _ = TelegramAPI.send_message(item['message'])
            if ok:
                sent += 1
            else:
                failed.append(item)

        cls._set(failed)
        return sent, len(failed)

    @classmethod
    def clear(cls):
        cache.delete(QUEUE_KEY)

    @classmethod
    def get_all(cls):
        return cls._get()

    @classmethod
    def _get(cls):
        data = cache.get(QUEUE_KEY)
        if data is None:
            return []
        if isinstance(data, str):
            try:
                return json.loads(data)
            except (json.JSONDecodeError, ValueError):
                return []
        return data

    @classmethod
    def _set(cls, queue):
        cache.set(QUEUE_KEY, queue, QUEUE_TTL)
