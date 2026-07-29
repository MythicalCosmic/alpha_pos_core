import json

from django.http import JsonResponse

from base.helpers.request import (
    SessionCredentialConflict,
    resolve_session_credential,
)
from base.repositories import SessionRepository
from base.security.auth import (
    _ua_matches,
    session_credential_conflict_response,
)


ACCOUNT_SWITCH_REQUIRES_LOGOUT_CODE = "account_switch_requires_logout"
ACCOUNT_SWITCH_REQUIRES_LOGOUT_MESSAGE = (
    "Log out before signing in as a different account."
)


def _account_switch_response():
    return JsonResponse(
        {
            "success": False,
            "message": ACCOUNT_SWITCH_REQUIRES_LOGOUT_MESSAGE,
            "code": ACCOUNT_SWITCH_REQUIRES_LOGOUT_CODE,
        },
        status=409,
    )


def _valid_current_user(request):
    """Return ``(current_user, error_response)`` for a login transition.

    This intentionally mirrors the session validity checks used by the normal
    request decorators.  An invalid, expired, suspended, or client-mismatched
    credential is not a still-authenticated browser session and therefore does
    not prevent a fresh login.
    """
    try:
        session_key, _source = resolve_session_credential(request)
    except SessionCredentialConflict:
        return None, session_credential_conflict_response()

    if not session_key:
        return None, None
    session = SessionRepository.get_by_session_key(session_key)
    user = getattr(session, 'user_id', None)
    if (
        not session
        or not user
        or user.is_deleted
        or user.status != 'ACTIVE'
        or session.is_expired()
        or not _ua_matches(session, request)
    ):
        return None, None
    return user, None


def _targets_current_user(request, current_user):
    """Whether an auth-login JSON body selects ``current_user``.

    POS login prefers a truthy ``user_id`` over email, exactly as its auth
    service does.  Admin and waiter login use email only.
    """
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None

    target_user_id = body.get('user_id')
    if target_user_id:
        try:
            return int(target_user_id) == int(current_user.pk)
        except (TypeError, ValueError):
            return False

    target_email = body.get('email')
    if target_email:
        return (
            str(target_email).strip().casefold()
            == str(current_user.email or '').strip().casefold()
        )
    return None


class LoginTransitionGuardMiddleware:
    """Require logout before an authenticated browser changes identities."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path.rstrip('/')
        if request.method == 'POST' and path == '/auth-login':
            return self.get_response(request)
        if request.method == 'POST' and path.endswith('/auth-login'):
            current_user, error_response = _valid_current_user(request)
            if error_response is not None:
                return error_response
            if current_user is not None:
                targets_current = _targets_current_user(request, current_user)
                if targets_current is False:
                    return _account_switch_response()
        return self.get_response(request)
