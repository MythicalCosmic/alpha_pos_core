from functools import wraps
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse


def _get_ip(request):
    # X-Forwarded-For is attacker-controlled when no reverse proxy strips it.
    # Trust it only when the operator has explicitly opted in via
    # TRUST_FORWARDED_FOR — otherwise an attacker can rotate the header to
    # bypass the per-IP rate limit (or stuff a victim's IP to lock them out).
    if getattr(settings, 'TRUST_FORWARDED_FOR', False):
        xff = request.META.get('HTTP_X_FORWARDED_FOR')
        if xff:
            return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _check_and_incr(key, max_attempts, window):
    """Returns retry_after seconds if the limit is exceeded, else None.

    Uses add()+incr() instead of get()-then-set(): incr is atomic on both
    LocMem and Redis, so two concurrent requests can't both read count<max and
    slip through the old check-then-set race. (A shared backend like Redis is
    still required to enforce the limit ACROSS worker processes — LocMem is
    per-process.)
    """
    # Seed the window only if absent; a no-op when the key already exists.
    cache.add(key, 0, window)
    try:
        count = cache.incr(key)
    except ValueError:
        # Key expired between add and incr — re-seed and count this request.
        cache.add(key, 0, window)
        count = cache.incr(key)
    if count > max_attempts:
        return cache.ttl(key) if hasattr(cache, 'ttl') else window
    return None


def rate_limit(key_prefix, max_attempts, window, error_payload=None):
    """Rate-limit a view by source IP.

    ``error_payload`` lets an endpoint provide its own user-ready error contract
    while preserving the historical response everywhere else.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            ip = _get_ip(request)
            retry_after = _check_and_incr(
                f"rl:{key_prefix}:{ip}", max_attempts, window,
            )
            if retry_after is not None:
                body = {"success": False, "message": "Too many requests"}
                if error_payload:
                    body.update(dict(error_payload))
                return JsonResponse(
                    body,
                    status=429,
                    headers={"Retry-After": str(retry_after)},
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def rate_limit_by(key_prefix, max_attempts, window, extractor):
    """Rate-limit by a request-derived key (e.g. username, phone, target id)
    in addition to IP. Use on auth endpoints to defeat distributed
    credential-stuffing where the attacker rotates source IPs.

    `extractor(request)` returns the key string, or None to skip the check.
    Combine with IP-based @rate_limit on the same view for layered defense.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            try:
                ident = extractor(request)
            except Exception:
                ident = None
            if ident:
                retry_after = _check_and_incr(
                    f"rl:{key_prefix}:by:{ident}", max_attempts, window,
                )
                if retry_after is not None:
                    return JsonResponse(
                        {"success": False, "message": "Too many requests"},
                        status=429,
                        headers={"Retry-After": str(retry_after)},
                    )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator

