import json
from django.conf import settings


def coerce_quantity(value, default=None):
    """Coerce a JSON order quantity to a positive int, or None if invalid.

    Order-item views did `if not quantity or quantity <= 0` on the raw JSON
    value, so a string like "5" sailed past `not "5"` and then raised
    TypeError on `"5" <= 0` — surfacing as a 500 instead of a clean 422.
    This accepts ints and integer-valued numeric strings/floats, and rejects
    bools, non-numeric strings, fractional floats, and anything <= 0.
    """
    if value is None:
        value = default
    if isinstance(value, bool):  # bool is a subclass of int — reject explicitly
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():  # only non-negative integer literals
            n = int(s)
            return n if n > 0 else None
        return None
    return None


def get_client_ip(request):
    # See base/security/rate_limit.py — same trust rule.
    if getattr(settings, 'TRUST_FORWARDED_FOR', False):
        xff = request.META.get('HTTP_X_FORWARDED_FOR')
        if xff:
            return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def get_user_agent(request):
    ua = request.META.get('HTTP_USER_AGENT', '')
    return ua[:256]


def get_session_key(request):
    key = request.COOKIES.get('session_key')
    if key:
        return key
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return None


def validate_pagination(request, default_per_page=20):
    from django.conf import settings
    max_per_page = getattr(settings, 'MAX_PER_PAGE', 100)
    try:
        page = max(1, int(request.GET.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = max(1, min(int(request.GET.get('per_page', default_per_page)), max_per_page))
    except (ValueError, TypeError):
        per_page = default_per_page
    return page, per_page


def safe_per_page(request, default=20):
    # Return per_page bounded by MAX_PER_PAGE, for callers that read page
    # separately. Use validate_pagination when both are needed.
    from django.conf import settings
    max_per_page = getattr(settings, 'MAX_PER_PAGE', 100)
    try:
        return max(1, min(int(request.GET.get('per_page', default)), max_per_page))
    except (ValueError, TypeError):
        return default


def safe_page(request, default=1):
    try:
        return max(1, int(request.GET.get('page', default)))
    except (ValueError, TypeError):
        return default


def safe_int(request, key, default=None, minimum=None, maximum=None):
    """Parse an integer query param without letting bad input crash the view.

    A bare `int(request.GET[...])` raises ValueError on non-numeric input,
    which surfaces as an HTTP 500 (the global JSON middleware masks the
    traceback but still returns 500). This returns `default` on missing or
    malformed input and clamps the result to [minimum, maximum] when given —
    bounding otherwise-unbounded work like `?limit=` / `?days=`.
    """
    raw = request.GET.get(key, None)
    if raw is None or raw == '':
        value = default
    else:
        try:
            value = int(raw)
        except (ValueError, TypeError):
            value = default
    if value is None:
        return None
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def safe_date(request, key, default=None):
    """Parse an ISO date/datetime query param to a date.

    Returns `default` on missing or malformed input instead of raising
    ValueError (→ HTTP 500) from a bare `datetime.fromisoformat(...)`.
    """
    from datetime import datetime
    raw = request.GET.get(key)
    if not raw:
        return default
    try:
        return datetime.fromisoformat(raw).date()
    except (ValueError, TypeError):
        return default


def parse_json_body(request):
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return None, ({"success": False, "message": "Expected JSON object"}, 400)
        return data, None
    except (json.JSONDecodeError, ValueError):
        return None, ({"success": False, "message": "Invalid JSON"}, 400)
