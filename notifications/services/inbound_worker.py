"""Process-local queue for Telegram updates that would block a web worker.

The webhook acknowledges queued updates immediately. A hard shutdown can lose
an acknowledged update, so this queue is an availability aid, not durable
delivery.
"""
import logging
import queue
import threading

logger = logging.getLogger(__name__)

# Bound the backlog so a flood (or a stuck handler) can't grow memory without
# limit. When full we drop the oldest-style by refusing the new one and let the
# caller fall back to inline handling.
_MAX_QUEUE = 1000

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=_MAX_QUEUE)
_worker_started = False
_worker_lock = threading.Lock()


def enqueue_update(update):
    """Queue a raw Telegram update for background handling.

    Returns True if queued, False if the backlog is full (caller should handle
    inline as a fallback so the update isn't silently dropped).
    """
    _ensure_worker()
    try:
        _queue.put_nowait(update)
        return True
    except queue.Full:
        logger.warning('Telegram inbound queue full (%s); handling inline', _MAX_QUEUE)
        return False


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(
            target=_worker_loop, name='telegram-inbound-worker', daemon=True,
        )
        t.start()
        _worker_started = True


def _worker_loop():
    from django.db import close_old_connections
    while True:
        try:
            update = _queue.get()
        except Exception:
            continue
        try:
            from notifications.services.telegram_bot import handle_update
            handle_update(update)
        except Exception:
            # A handler bug must not kill the worker thread — log and move on.
            logger.exception(
                'Telegram bot handler crashed on update %s',
                update.get('update_id') if isinstance(update, dict) else '?',
            )
        finally:
            # Release this thread's DB connection each update — see worker.py.
            close_old_connections()
            _queue.task_done()
