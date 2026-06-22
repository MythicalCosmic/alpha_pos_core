import json
import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)

QUEUE_KEY = 'notif:pending'
QUEUE_TTL = 86400
# Hard cap on the pending list. Under a prolonged Telegram outage every send
# would otherwise append forever and the cache value would grow without bound.
# When full we drop the OLDEST entries (head of the list) so the newest
# notifications survive.
MAX_PENDING = 500


class QueueService:

    @classmethod
    def add(cls, message, notification_type, chat_ids=None, order_id=None, thread_role=None):
        queue = cls._get()
        queue.append({
            'message': message,
            'type': notification_type,
            # Specific chats still pending delivery. None == all configured
            # chats. Storing this lets process() retry only the failed chats.
            'chat_ids': chat_ids,
            # Order-notification threading metadata (best-effort on retry).
            'order_id': order_id,
            'thread_role': thread_role,
        })
        if len(queue) > MAX_PENDING:
            dropped = len(queue) - MAX_PENDING
            queue = queue[dropped:]
            logger.warning(
                'Notification queue full (>%d); dropped %d oldest pending item(s)',
                MAX_PENDING, dropped,
            )
        cls._set(queue)
        logger.info(f'Queued notification: {notification_type}')

    @classmethod
    def count(cls):
        return len(cls._get())

    @classmethod
    def process(cls):
        from notifications.services.telegram_service import TelegramService

        if not TelegramService.is_online():
            return 0, 0

        queue = cls._get()
        if not queue:
            return 0, 0

        config = TelegramService._get_config()
        sent = 0
        failed = []

        for item in queue:
            # Target only the chats still pending for this item (or all
            # configured chats for legacy items without chat_ids), and re-queue
            # only the chats that fail again — never re-send to a chat that
            # already received the message.
            targets = item.get('chat_ids')
            if targets is None:
                targets = config.chat_ids
            reply_to = None
            if item.get('thread_role') == 'reply' and item.get('order_id'):
                from notifications.services.worker import _new_message_ids
                reply_to = _new_message_ids(item['order_id'])
            still_failed, _, sent_ids = TelegramService.send_to_chats(
                item['message'], targets, reply_to=reply_to)
            if item.get('thread_role') == 'new' and item.get('order_id') and sent_ids:
                from notifications.services.worker import _store_message_ids
                _store_message_ids(item['order_id'], sent_ids)
            if not still_failed:
                sent += 1
            else:
                failed.append({**item, 'chat_ids': still_failed})

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
