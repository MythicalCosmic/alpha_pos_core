"""Single-worker notification dispatcher with token-bucket rate limiting.

Telegram throttles aggressively (30 messages/sec global, 1/sec/chat). The
previous design spawned one daemon thread per notification, so a burst of
orders could spawn dozens of threads each doing a blocking HTTPS POST and
trip Telegram's rate limit.

This module exposes a process-wide queue drained by a single background
thread. The thread enforces a per-chat minimum interval before each send.

Limitations:
- The rate limiter is per-process. With N gunicorn workers you can issue
  up to N messages/sec/chat. For a single-branch POS with a small worker
  count this is acceptable; a Redis-backed token bucket would be needed
  to coordinate across processes.
- Queued messages in the in-memory queue are lost on hard shutdown.
"""
import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)

# Telegram's stated per-chat limit. Tuneable via settings.NOTIF_MIN_CHAT_INTERVAL.
DEFAULT_MIN_CHAT_INTERVAL = 1.0

_queue: "queue.Queue[dict]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_last_send_per_chat: dict[str, float] = {}


def enqueue(text, notification_type, order_id=None, thread_role=None):
    """Fire-and-forget enqueue. The worker is lazily started on first call.

    order_id + thread_role drive order-notification reply threading:
      thread_role='new'   -> after sending, store the per-chat message ids on the
                             order's OrderNotificationDispatch row.
      thread_role='reply' -> before sending, load those stored ids and send THIS
                             message as a reply threaded under the order.new message.
    """
    _ensure_worker()
    _queue.put({
        'text': text, 'notification_type': notification_type,
        'order_id': order_id, 'thread_role': thread_role,
    })


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, name='notif-worker', daemon=True)
        t.start()
        _worker_started = True


def _worker_loop():
    from django.conf import settings as django_settings
    min_interval = getattr(
        django_settings, 'NOTIF_MIN_CHAT_INTERVAL', DEFAULT_MIN_CHAT_INTERVAL
    )

    from django.db import close_old_connections
    while True:
        try:
            item = _queue.get()
        except Exception:
            continue
        try:
            _dispatch(item, min_interval)
        except Exception:
            logger.exception('notification worker dispatch failed')
        finally:
            # This daemon thread runs ORM queries (NotificationSettings.load,
            # NotificationLog.create) outside the request cycle, so nothing
            # releases its DB connection. Close it after each item so it doesn't
            # pin a connection (Postgres "too many clients" / SQLite WAL slot)
            # while idle between bursts.
            close_old_connections()
            _queue.task_done()


def _dispatch(item, min_interval):
    from notifications.models import NotificationSettings, NotificationLog
    from notifications.services.telegram_service import TelegramService
    from notifications.services.queue_service import QueueService

    settings = NotificationSettings.load()
    text = item['text']
    notification_type = item['notification_type']
    order_id = item.get('order_id')
    thread_role = item.get('thread_role')

    # Per-chat routing: only the chats subscribed to this message's category
    # receive it (managed in the desktop panel). Manual test sends ('test') and
    # any explicit re-queued chat list bypass routing and go to their targets.
    explicit = item.get('chat_ids')
    if explicit:
        chat_ids = [str(c) for c in explicit]
    elif notification_type == 'test':
        chat_ids = settings.chat_ids or []
    else:
        chat_ids = settings.recipients_for(notification_type)
    if not chat_ids:
        return

    # Throttle per chat. We don't actually iterate per chat in TelegramService
    # — it sends to all chats in one call — so we throttle on the first
    # chat_id which is good enough for a small-fanout POS deployment.
    chat_key = str(chat_ids[0])
    now = time.monotonic()
    last = _last_send_per_chat.get(chat_key, 0.0)
    delta = now - last
    if delta < min_interval:
        time.sleep(min_interval - delta)

    # Reply threading: load the order.new message ids so this 'reply' message
    # is sent threaded under the original order.new message in each chat.
    reply_to = None
    if thread_role == 'reply' and order_id:
        reply_to = _new_message_ids(order_id)

    try:
        failed, error, sent_ids = TelegramService.send_to_chats(
            text, chat_ids, reply_to=reply_to)
        ok = not failed
        _last_send_per_chat[chat_key] = time.monotonic()
        # Persist the order.new message ids per chat for later reply threading.
        if thread_role == 'new' and order_id and sent_ids:
            _store_message_ids(order_id, sent_ids)
        _write_log(
            notification_type=notification_type,
            recipient=','.join(str(c) for c in chat_ids),
            message_text=text,
            status='SENT' if ok else 'FAILED',
            error_message=error if not ok else '',
        )
        if failed:
            # Re-queue ONLY the chats that failed so the retry doesn't duplicate
            # the message to chats that already received it.
            QueueService.add(text, notification_type, chat_ids=failed,
                             order_id=order_id, thread_role=thread_role)
    except Exception as e:
        _write_log(
            notification_type=notification_type,
            recipient=','.join(str(c) for c in chat_ids),
            message_text=text,
            status='FAILED',
            error_message=str(e),
        )
        QueueService.add(text, notification_type,
                         order_id=order_id, thread_role=thread_role)


def _write_log(**fields):
    """Keep notification observability failures out of delivery semantics.

    A Telegram send that succeeded must not be re-queued (and duplicated) just
    because its audit-log insert failed. The DB/schema problem is logged for
    operators while transport retries remain driven only by transport results.
    """
    try:
        from notifications.models import NotificationLog
        NotificationLog.objects.create(**fields)
    except Exception:
        logger.exception('failed to persist notification delivery log')


def _new_message_ids(order_id):
    """The {chat_id: message_id} of the order.new message, or None."""
    try:
        from notifications.models import OrderNotificationDispatch
        disp = OrderNotificationDispatch.objects.filter(order_id=order_id).first()
        if disp and disp.new_message_ids:
            return {str(k): v for k, v in disp.new_message_ids.items()}
    except Exception:
        logger.debug('failed to load new_message_ids for order %s', order_id, exc_info=True)
    return None


def _store_message_ids(order_id, sent_ids):
    """Merge the just-sent order.new message ids onto the dispatch row."""
    try:
        from notifications.models import OrderNotificationDispatch
        disp, _ = OrderNotificationDispatch.objects.get_or_create(order_id=order_id)
        merged = dict(disp.new_message_ids or {})
        merged.update({str(k): v for k, v in sent_ids.items()})
        disp.new_message_ids = merged
        disp.save(update_fields=['new_message_ids', 'updated_at'])
    except Exception:
        logger.debug('failed to store message ids for order %s', order_id, exc_info=True)
