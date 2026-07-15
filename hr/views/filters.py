"""Small, consistent query-parameter normalizers for HR list endpoints."""

from django.utils.dateparse import parse_date


def query_value(request, *names):
    """Return the first non-empty query value among canonical names/aliases."""
    for name in names:
        value = request.GET.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return None


def query_enum(request, *names):
    value = query_value(request, *names)
    if value is None or value.lower() == "all":
        return None
    return value.upper()


def query_int(request, *names):
    value = query_value(request, *names)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def query_bool(request, *names):
    value = query_value(request, *names)
    if value is None:
        return None
    normalized = value.lower()
    if normalized in {"true", "1", "yes", "active", "enabled"}:
        return True
    if normalized in {"false", "0", "no", "inactive", "disabled"}:
        return False
    # Values such as "all" mean no status filter in the frontend.
    return None


def query_date(request, *names):
    value = query_value(request, *names)
    if not value:
        return None
    try:
        return parse_date(value)
    except (TypeError, ValueError):
        return None


def query_date_range(request):
    """Support both an exact ``date`` and the canonical date range fields."""
    exact = query_date(request, "date")
    return (
        query_date(request, "date_from", "start_date") or exact,
        query_date(request, "date_to", "end_date") or exact,
    )
