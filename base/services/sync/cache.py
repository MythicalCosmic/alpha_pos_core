import logging
import threading

logger = logging.getLogger(__name__)

_fallback = {}
_fallback_ttl = {}
_fallback_lock = threading.Lock()
_available = None
_available_checked = 0


def _cache():
    from django.core.cache import cache
    return cache


def is_available():
    global _available, _available_checked
    import time
    now = time.time()
    if _available is not None and now - _available_checked < 30:
        return _available

    try:
        _cache().set('sync:ping', 1, 10)
        val = _cache().get('sync:ping')
        _available = val == 1
    except Exception:
        _available = False

    _available_checked = now

    if _available:
        _flush_fallback()

    return _available


def safe_get(key, default=None):
    try:
        val = _cache().get(key)
        if val is not None:
            return val
        with _fallback_lock:
            return _fallback.get(key, default)
    except Exception:
        with _fallback_lock:
            return _fallback.get(key, default)


def safe_set(key, value, ttl=None):
    with _fallback_lock:
        _fallback[key] = value
        _fallback_ttl[key] = ttl
    try:
        _cache().set(key, value, ttl)
    except Exception:
        logger.debug(f'Redis unavailable, using fallback for {key}')


def safe_delete(key):
    with _fallback_lock:
        _fallback.pop(key, None)
        _fallback_ttl.pop(key, None)
    try:
        _cache().delete(key)
    except Exception:
        logger.debug(f'Redis unavailable on delete for {key}')


def safe_add(key, value, ttl):
    try:
        return _cache().add(key, value, ttl)
    except Exception:
        with _fallback_lock:
            if key in _fallback:
                return False
            _fallback[key] = value
            _fallback_ttl[key] = ttl
            return True


def _flush_fallback():
    global _fallback
    with _fallback_lock:
        if not _fallback:
            return
        items = dict(_fallback)

    flushed = 0
    for key, value in items.items():
        try:
            # Preserve each key's original TTL — flushing with None would make
            # a short-lived lock or status entry permanent in Redis.
            with _fallback_lock:
                ttl = _fallback_ttl.get(key)
            _cache().set(key, value, ttl)
            flushed += 1
        except Exception:
            break

    if flushed > 0:
        logger.info(f'Flushed {flushed} fallback entries to Redis')
