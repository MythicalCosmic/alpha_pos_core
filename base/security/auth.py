from functools import wraps
from django.http import JsonResponse
from base.helpers.request import get_session_key, get_user_agent
from base.repositories import SessionRepository


def _ua_matches(session, request) -> bool:
    stored = (session.user_agent or '').strip()
    return stored == get_user_agent(request).strip()


def login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        session_key = get_session_key(request)
        if not session_key:
            return JsonResponse(
                {"success": False, "message": "Authentication required"},
                status=401,
            )
        session = SessionRepository.get_by_session_key(session_key)
        if not session or not session.user_id or session.user_id.is_deleted:
            return JsonResponse(
                {"success": False, "message": "Invalid or expired session"},
                status=401,
            )
        if session.user_id.status != 'ACTIVE':
            SessionRepository.invalidate_cache(session_key)
            SessionRepository.delete(session)
            return JsonResponse(
                {"success": False, "message": "Account is not active"},
                status=403,
            )
        if session.is_expired():
            SessionRepository.invalidate_cache(session_key)
            SessionRepository.delete(session)
            return JsonResponse(
                {"success": False, "message": "Invalid or expired session"},
                status=401,
            )
        if not _ua_matches(session, request):
            return JsonResponse(
                {"success": False, "message": "Session client mismatch"},
                status=401,
            )
        request.user = session.user_id
        request.session_key = session_key
        return view_func(request, *args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not hasattr(request, 'user') or request.user is None:
                return JsonResponse(
                    {"success": False, "message": "Authentication required"},
                    status=401,
                )
            if request.user.role not in roles:
                return JsonResponse(
                    {"success": False, "message": "Insufficient permissions"},
                    status=403,
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
