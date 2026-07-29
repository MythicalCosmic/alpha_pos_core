"""Durable retry queue for staff Telegram notifications.

Production uses Django's Redis cache.  Queue state is stored as two lists:

* ``QUEUE_KEY`` receives new work.
* ``PROCESSING_KEY`` owns the batch currently claimed by the single retry pass.

The state lock is held only while moving/merging lists, never while calling
Telegram or PostgreSQL.  New web-worker additions therefore remain available
while a retry pass is running and cannot be overwritten by that pass's final
write.  A separate process lock prevents the sidecar and the manual admin
endpoint from draining the same batch concurrently.  If a process dies after
claiming, the claimed batch remains in ``PROCESSING_KEY`` and the next process
replays it (at-least-once, never silently lost).
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import time
import uuid

from django.core.cache import cache


logger = logging.getLogger(__name__)

QUEUE_KEY = 'notif:pending'
PROCESSING_KEY = 'notif:processing'
DEAD_LETTER_KEY = 'notif:dead'
STATE_LOCK_KEY = 'notif:state-lock'
PROCESS_LOCK_KEY = 'notif:process-lock'

QUEUE_TTL = 86400
DEAD_LETTER_TTL = 7 * 86400
STATE_LOCK_TTL = 30
PROCESS_LOCK_TTL = 15 * 60
LOCK_WAIT_SECONDS = 5

# Bound both work and recovery time.  At Telegram's 10-second timeout a batch
# cannot hold the process lease for longer than roughly eight minutes.
MAX_PROCESS_BATCH = 50
MAX_TELEGRAM_REQUESTS_PER_BATCH = 30
MAX_PENDING = 500
MAX_DEAD_LETTERS = 500

MAX_ATTEMPTS = 8
BASE_RETRY_SECONDS = 15
MAX_RETRY_SECONDS = 15 * 60


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_epoch():
    return time.time()


class QueueService:

    @classmethod
    def add(
        cls,
        message,
        notification_type,
        chat_ids=None,
        order_id=None,
        thread_role=None,
        *,
        attempts=0,
        last_error='',
        next_attempt_at=None,
        sent_ids=None,
        recipient_ids=None,
    ):
        """Append one retry item without racing another web/worker process.

        Extra keyword-only fields support ``thread_role='persist'``: Telegram
        already accepted the root message, so only its returned message ids and
        frozen audience are retried.  The root message is never sent twice.
        """
        item = {
            'message': message,
            'type': notification_type,
            # Specific chats still pending delivery. None == resolve later.
            'chat_ids': chat_ids,
            'order_id': order_id,
            'thread_role': thread_role,
            'attempts': max(0, int(attempts or 0)),
            'last_error': str(last_error or ''),
            'next_attempt_at': (
                float(next_attempt_at)
                if next_attempt_at is not None
                else 0.0
            ),
        }
        if sent_ids:
            item['sent_ids'] = {
                str(chat_id): message_id
                for chat_id, message_id in sent_ids.items()
            }
        if recipient_ids:
            item['recipient_ids'] = cls._unique_chat_ids(recipient_ids)

        with cls._state_lock():
            pending = cls._read_list(QUEUE_KEY)
            processing_count = len(cls._read_list(PROCESSING_KEY))
            pending.append(item)
            available = max(0, MAX_PENDING - processing_count)
            if len(pending) > available:
                dropped = len(pending) - available
                pending = pending[dropped:]
                logger.warning(
                    'Notification queue full (>%d including claimed work); '
                    'dropped %d oldest pending item(s)',
                    MAX_PENDING,
                    dropped,
                )
            cls._write_list(QUEUE_KEY, pending, QUEUE_TTL)
        logger.info('Queued notification retry: %s (%s)', notification_type, thread_role)

    @classmethod
    def count(cls):
        with cls._state_lock():
            return (
                len(cls._read_list(PROCESSING_KEY))
                + len(cls._read_list(QUEUE_KEY))
            )

    @classmethod
    def dead_letter_count(cls):
        with cls._state_lock():
            return len(cls._read_list(DEAD_LETTER_KEY))

    @classmethod
    def process(cls):
        """Process one claimed retry batch.

        Returns ``(completed, pending_count)``.  Only one process can own a pass;
        another caller receives ``(0, current_count)`` without touching state.
        """
        with cls._process_lock() as acquired:
            if not acquired:
                return 0, cls.count()

            batch = cls._claim_batch()
            if not batch:
                return 0, cls.count()

            completed = 0
            retry_items = []
            dead_letters = []
            now = _now_epoch()

            for raw_item in batch:
                item = cls._normalize_item(raw_item)
                if float(item.get('next_attempt_at') or 0) > now:
                    retry_items.append(item)
                    continue

                try:
                    outcome = cls._process_item(item)
                except Exception as exc:  # one malformed item must not replay successes
                    logger.exception('notification retry item failed unexpectedly')
                    outcome = {
                        'ok': False,
                        'error': str(exc),
                        'failed_chat_ids': item.get('chat_ids'),
                        'followups': [],
                    }

                # Persistence-only followups must remain ahead of later work:
                # an edit can use the root id immediately after it is stored.
                retry_items.extend(outcome.get('followups') or [])

                if outcome.get('ok'):
                    completed += 1
                    continue

                failed_ids = outcome.get('failed_chat_ids')
                if failed_ids is not None:
                    item['chat_ids'] = cls._unique_chat_ids(failed_ids)
                error = str(outcome.get('error') or 'Delivery failed')
                item['last_error'] = error
                item['attempts'] = int(item.get('attempts') or 0) + 1

                # Telegram's batch helpers preserve API compatibility by
                # returning one aggregate last_error.  A multi-chat attempt can
                # contain both a permanent 400 and a transient timeout, so do
                # not prematurely dead-letter every failed chat from that one
                # string.  All failures use bounded exponential retry and are
                # dead-lettered deterministically at MAX_ATTEMPTS.
                if item['attempts'] >= MAX_ATTEMPTS:
                    item['dead_lettered_at'] = _utc_now_iso()
                    item['dead_letter_reason'] = error
                    dead_letters.append(item)
                    logger.error(
                        'Dead-lettered Telegram notification type=%s role=%s '
                        'order=%s attempts=%s error=%s',
                        item.get('type'),
                        item.get('thread_role'),
                        item.get('order_id'),
                        item['attempts'],
                        error,
                    )
                    continue

                item['next_attempt_at'] = (
                    _now_epoch() + cls._retry_delay(item['attempts'])
                )
                retry_items.append(item)

            cls._complete_batch(retry_items, dead_letters)
            return completed, cls.count()

    @classmethod
    def _process_item(cls, item):
        from notifications.services.telegram_service import TelegramService
        from notifications.services.worker import (
            _new_message_ids,
            _new_recipient_ids,
            _store_message_ids,
            _store_recipient_ids,
        )

        notification_type = item.get('type') or ''
        thread_role = item.get('thread_role')
        order_id = item.get('order_id')

        # Rolling upgrade: pre-0012 READY failures were queued as replies.
        # Convert them before transport so deployment never emits the old second
        # message after the one-message lifecycle feature goes live.
        if notification_type == 'order.ready' and thread_role == 'reply':
            thread_role = 'edit'
            item['thread_role'] = 'edit'

        if thread_role == 'persist':
            sent_ids = item.get('sent_ids') or {}
            recipient_ids = (
                item.get('recipient_ids')
                or item.get('chat_ids')
                or list(sent_ids)
            )
            audience_ok = _store_recipient_ids(order_id, recipient_ids)
            ids_ok = bool(sent_ids) and _store_message_ids(order_id, sent_ids)
            if audience_ok and ids_ok:
                return {'ok': True, 'followups': []}
            return {
                'ok': False,
                'error': 'Database persistence for Telegram message ids failed',
                'failed_chat_ids': item.get('chat_ids'),
                'followups': [],
            }

        targets = item.get('chat_ids')
        if targets is None and thread_role == 'edit' and order_id:
            targets = _new_recipient_ids(order_id)
            if not targets:
                targets = list((_new_message_ids(order_id) or {}).keys())
        if targets is None:
            targets = TelegramService._get_config().chat_ids
        targets = cls._unique_chat_ids(targets)
        if not targets:
            return {
                'ok': False,
                'error': 'No Telegram recipients available for retry',
                'failed_chat_ids': [],
                'followups': [],
            }

        if thread_role == 'new' and order_id:
            if not _store_recipient_ids(order_id, targets):
                return {
                    'ok': False,
                    'error': 'Database persistence for Telegram audience failed',
                    'failed_chat_ids': targets,
                    'followups': [],
                }

        if thread_role == 'edit' and order_id:
            message_ids = _new_message_ids(order_id) or {}
            failed, error = TelegramService.edit_in_chats(
                item.get('message') or '',
                message_ids,
                chat_ids=targets,
            )
            return {
                'ok': not failed,
                'error': error,
                'failed_chat_ids': failed,
                'followups': [],
            }

        reply_to = (
            _new_message_ids(order_id)
            if thread_role == 'reply' and order_id
            else None
        )
        failed, error, sent_ids = TelegramService.send_to_chats(
            item.get('message') or '',
            targets,
            reply_to=reply_to,
        )

        followups = []
        if thread_role == 'new' and order_id and sent_ids:
            if not _store_message_ids(order_id, sent_ids):
                followups.append(cls._persistence_item(
                    item,
                    sent_ids=sent_ids,
                    recipient_ids=targets,
                ))

        return {
            'ok': not failed,
            'error': error,
            'failed_chat_ids': failed,
            'followups': followups,
        }

    @classmethod
    def _persistence_item(cls, source, *, sent_ids, recipient_ids):
        return {
            'message': source.get('message') or '',
            'type': source.get('type') or '',
            'chat_ids': cls._unique_chat_ids(recipient_ids),
            'order_id': source.get('order_id'),
            'thread_role': 'persist',
            'sent_ids': {
                str(chat_id): message_id
                for chat_id, message_id in sent_ids.items()
            },
            'recipient_ids': cls._unique_chat_ids(recipient_ids),
            'attempts': 0,
            'last_error': 'Telegram delivered; message-id persistence pending',
            'next_attempt_at': 0.0,
        }

    @classmethod
    def _normalize_item(cls, raw_item):
        item = dict(raw_item or {})
        item.setdefault('message', '')
        item.setdefault('type', '')
        item.setdefault('chat_ids', None)
        item.setdefault('order_id', None)
        item.setdefault('thread_role', None)
        item['attempts'] = max(0, int(item.get('attempts') or 0))
        item['last_error'] = str(item.get('last_error') or '')
        try:
            item['next_attempt_at'] = float(item.get('next_attempt_at') or 0)
        except (TypeError, ValueError):
            item['next_attempt_at'] = 0.0
        return item

    @staticmethod
    def _retry_delay(attempts):
        exponent = max(0, int(attempts) - 1)
        return min(MAX_RETRY_SECONDS, BASE_RETRY_SECONDS * (2 ** exponent))

    @classmethod
    def clear(cls):
        # Do not let a manual clear race an active processor's final merge.
        with cls._process_lock(blocking_timeout=LOCK_WAIT_SECONDS) as acquired:
            if not acquired:
                raise RuntimeError('Notification retry processor is busy')
            with cls._state_lock():
                cache.delete(QUEUE_KEY)
                cache.delete(PROCESSING_KEY)

    @classmethod
    def clear_dead_letters(cls):
        with cls._state_lock():
            cache.delete(DEAD_LETTER_KEY)

    @classmethod
    def get_all(cls):
        with cls._state_lock():
            return [
                *cls._read_list(PROCESSING_KEY),
                *cls._read_list(QUEUE_KEY),
            ]

    @classmethod
    def get_dead_letters(cls):
        with cls._state_lock():
            return cls._read_list(DEAD_LETTER_KEY)

    @classmethod
    def _claim_batch(cls):
        with cls._state_lock():
            claimed = cls._read_list(PROCESSING_KEY)
            if claimed:
                return claimed

            pending = cls._read_list(QUEUE_KEY)
            if not pending:
                return []

            # A retry whose backoff is still in the future must not block newer
            # due work behind it.  Select due items across the whole bounded
            # queue while preserving the relative order of both selected and
            # deferred items.
            now = _now_epoch()
            claimed = []
            remaining = []
            request_cost = 0
            for item in pending:
                normalized = cls._normalize_item(item)
                is_due = normalized['next_attempt_at'] <= now
                item_cost = cls._telegram_request_cost(normalized)
                fits_request_budget = (
                    request_cost + item_cost
                    <= MAX_TELEGRAM_REQUESTS_PER_BATCH
                )
                if (
                    is_due
                    and len(claimed) < MAX_PROCESS_BATCH
                    and fits_request_budget
                ):
                    claimed.append(normalized)
                    request_cost += item_cost
                else:
                    remaining.append(item)

            if not claimed:
                return []

            # No expiry: if the process dies, the next lease owner must replay
            # the claimed work rather than silently lose it.
            cls._write_list(PROCESSING_KEY, claimed, timeout=None)
            cls._write_list(QUEUE_KEY, remaining, QUEUE_TTL)
            return claimed

    @classmethod
    def _telegram_request_cost(cls, item):
        """Conservative HTTP-call count used to keep the Redis lease valid.

        New retry items freeze ``chat_ids`` before enqueueing.  A legacy item
        may omit them; reserve the entire request budget for that item so it
        cannot be combined with other Telegram sends.  Persistence-only retries
        perform no Telegram request.
        """
        if item.get('thread_role') == 'persist':
            return 0
        if item.get('chat_ids') is None:
            return MAX_TELEGRAM_REQUESTS_PER_BATCH
        return min(
            MAX_TELEGRAM_REQUESTS_PER_BATCH,
            max(1, len(cls._unique_chat_ids(item.get('chat_ids')))),
        )

    @classmethod
    def _complete_batch(cls, retry_items, dead_letters):
        with cls._state_lock():
            pending = cls._read_list(QUEUE_KEY)
            merged = [*retry_items, *pending]
            if len(merged) > MAX_PENDING:
                dropped = len(merged) - MAX_PENDING
                merged = merged[dropped:]
                logger.warning(
                    'Notification queue full while merging retries; dropped %d '
                    'oldest pending item(s)',
                    dropped,
                )
            cls._write_list(QUEUE_KEY, merged, QUEUE_TTL)

            if dead_letters:
                existing_dead = cls._read_list(DEAD_LETTER_KEY)
                combined_dead = [*existing_dead, *dead_letters]
                cls._write_list(
                    DEAD_LETTER_KEY,
                    combined_dead[-MAX_DEAD_LETTERS:],
                    DEAD_LETTER_TTL,
                )
            cache.delete(PROCESSING_KEY)

    @classmethod
    @contextmanager
    def _state_lock(cls):
        with cls._distributed_lock(
            STATE_LOCK_KEY,
            timeout=STATE_LOCK_TTL,
            blocking_timeout=LOCK_WAIT_SECONDS,
        ) as acquired:
            if not acquired:
                raise RuntimeError('Could not acquire notification queue state lock')
            yield

    @classmethod
    @contextmanager
    def _process_lock(cls, blocking_timeout=0):
        with cls._distributed_lock(
            PROCESS_LOCK_KEY,
            timeout=PROCESS_LOCK_TTL,
            blocking_timeout=blocking_timeout,
        ) as acquired:
            yield acquired

    @staticmethod
    @contextmanager
    def _distributed_lock(key, *, timeout, blocking_timeout):
        """Use django-redis's token-safe lock, with a cache.add fallback.

        LocMemCache (tests/local development) has no ``lock`` method, but its
        atomic ``add`` still serializes threads in one process.  Production
        Redis takes the first path and therefore coordinates every uvicorn and
        sidecar process.
        """
        lock_factory = getattr(cache, 'lock', None)
        if callable(lock_factory):
            lock = lock_factory(
                key,
                timeout=timeout,
                blocking_timeout=blocking_timeout,
            )
            acquired = lock.acquire(
                blocking=bool(blocking_timeout),
                blocking_timeout=blocking_timeout or None,
            )
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    try:
                        lock.release()
                    except Exception:
                        logger.exception('Failed to release Redis lock %s', key)
            return

        token = uuid.uuid4().hex
        deadline = time.monotonic() + max(0, float(blocking_timeout or 0))
        acquired = False
        while True:
            acquired = bool(cache.add(key, token, timeout=timeout))
            if acquired or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        try:
            yield acquired
        finally:
            # Critical sections are deliberately tiny and cannot approach the
            # lock TTL, so compare-then-delete is safe on fallback backends.
            if acquired and cache.get(key) == token:
                cache.delete(key)

    @staticmethod
    def _unique_chat_ids(chat_ids):
        normalized = (
            str(value).strip()
            for value in (chat_ids or [])
            if value is not None
        )
        return list(dict.fromkeys(value for value in normalized if value))

    @staticmethod
    def _read_list(key):
        data = cache.get(key)
        if data is None:
            return []
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                return []
        return list(data) if isinstance(data, (list, tuple)) else []

    @staticmethod
    def _write_list(key, values, timeout):
        cache.set(key, list(values), timeout=timeout)
