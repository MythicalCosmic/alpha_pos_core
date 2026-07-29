import logging
import threading

logger = logging.getLogger(__name__)

_fallback = {}
_fallback_ttl = {}
_fallback_lock = threading.Lock()
_available = None
_available_checked = 0


def _fallback_get_locked(key, default=None):
    import time

    expires_at = _fallback_ttl.get(key)
    if expires_at is not None and expires_at <= time.monotonic():
        _fallback.pop(key, None)
        _fallback_ttl.pop(key, None)
        return default
    return _fallback.get(key, default)


def _fallback_expiry(ttl):
    if ttl is None:
        return None
    import time
    return time.monotonic() + max(0, float(ttl))


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
            return _fallback_get_locked(key, default)
    except Exception:
        with _fallback_lock:
            return _fallback_get_locked(key, default)


def safe_set(key, value, ttl=None):
    with _fallback_lock:
        _fallback[key] = value
        _fallback_ttl[key] = _fallback_expiry(ttl)
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
            missing = object()
            if _fallback_get_locked(key, missing) is not missing:
                return False
            _fallback[key] = value
            _fallback_ttl[key] = _fallback_expiry(ttl)
            return True


def _flush_fallback():
    global _fallback
    with _fallback_lock:
        if not _fallback:
            return
        missing = object()
        for key in list(_fallback):
            _fallback_get_locked(key, missing)
        items = dict(_fallback)

    flushed = 0
    for key, value in items.items():
        try:
            # Preserve each key's original TTL — flushing with None would make
            # a short-lived lock or status entry permanent in Redis.
            with _fallback_lock:
                expires_at = _fallback_ttl.get(key)
            if expires_at is None:
                ttl = None
            else:
                import time
                ttl = max(0, expires_at - time.monotonic())
                if ttl <= 0:
                    safe_delete(key)
                    continue
            _cache().set(key, value, ttl)
            with _fallback_lock:
                if _fallback.get(key) == value:
                    _fallback.pop(key, None)
                    _fallback_ttl.pop(key, None)
            flushed += 1
        except Exception:
            break

    if flushed > 0:
        logger.info(f'Flushed {flushed} fallback entries to Redis')
