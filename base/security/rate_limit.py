from functools import wraps
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse


def _get_ip(request):
    if getattr(settings, 'TRUST_FORWARDED_FOR', False):
        xff = request.META.get('HTTP_X_FORWARDED_FOR')
        if xff:
            return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _check_and_incr(key, max_attempts, window):

    cache.add(key, 0, window)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.add(key, 0, window)
        count = cache.incr(key)
    if count > max_attempts:
        return cache.ttl(key) if hasattr(cache, 'ttl') else window
    return None


def rate_limit(key_prefix, max_attempts, window, error_payload=None):
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


def rate_limit_by(key_prefix, max_attempts, window, extractor, error_payload=None):
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

