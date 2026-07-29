import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.http import JsonResponse
from django.test import RequestFactory
from django.utils import timezone

from base.helpers.request import (
    SESSION_CREDENTIAL_CONFLICT_CODE,
    SESSION_CREDENTIAL_CONFLICT_MESSAGE,
    SessionCredentialConflict,
    resolve_session_credential,
)
from base.helpers.websocket import resolve_websocket_session_credential
from base.middlewares.login_transition_guard import (
    ACCOUNT_SWITCH_REQUIRES_LOGOUT_CODE,
    ACCOUNT_SWITCH_REQUIRES_LOGOUT_MESSAGE,
    LoginTransitionGuardMiddleware,
)
from base.models import Session, User
from base.repositories import SessionRepository
from base.security.auth import login_required
from base.security.permissions import admin_required


pytestmark = pytest.mark.django_db

TOKEN_A = 'a' * 64
TOKEN_B = 'b' * 64
USER_AGENT = 'session-hardening-test'


def _user(email, role=User.RoleChoices.CASHIER):
    return User.objects.create(
        first_name='Auth',
        last_name='Tester',
        email=email,
        password='!',
        role=role,
        status=User.UserStatus.ACTIVE,
    )


def _session(user, token=TOKEN_A, *, expired=False):
    return Session.objects.create(
        user_id=user,
        payload=SessionRepository.hash_token(token),
        ip_address='127.0.0.1',
        user_agent=USER_AGENT,
        expires_at=timezone.now() + (
            -timedelta(minutes=1) if expired else timedelta(hours=1)
        ),
    )


def _request(*, cookie=None, bearer=None, method='get', path='/protected', data=None):
    headers = {'HTTP_USER_AGENT': USER_AGENT}
    if bearer is not None:
        headers['HTTP_AUTHORIZATION'] = f'Bearer {bearer}'
    factory = RequestFactory()
    if method == 'post':
        request = factory.post(
            path,
            data=json.dumps(data or {}),
            content_type='application/json',
            **headers,
        )
    else:
        request = factory.get(path, **headers)
    if cookie is not None:
        request.COOKIES['session_key'] = cookie
    return request


@pytest.mark.parametrize(
    ('cookie', 'bearer', 'expected_source'),
    [
        (TOKEN_A, None, 'cookie'),
        (None, TOKEN_A, 'bearer'),
        (TOKEN_A, TOKEN_A, 'cookie+bearer'),
    ],
)
def test_session_credential_source_is_explicit(cookie, bearer, expected_source):
    key, source = resolve_session_credential(
        _request(cookie=cookie, bearer=bearer),
    )
    assert key == TOKEN_A
    assert source == expected_source


def test_different_cookie_and_bearer_fail_without_leaking_tokens():
    with pytest.raises(SessionCredentialConflict) as caught:
        resolve_session_credential(
            _request(cookie=TOKEN_A, bearer=TOKEN_B),
        )

    rendered = f'{caught.value!r} {caught.value}'
    assert str(caught.value) == SESSION_CREDENTIAL_CONFLICT_MESSAGE
    assert TOKEN_A not in rendered
    assert TOKEN_B not in rendered


def test_non_ascii_dual_credentials_take_stable_conflict_path():
    with pytest.raises(SessionCredentialConflict) as caught:
        resolve_session_credential(
            _request(cookie='not-a-token-\N{SNOWMAN}', bearer=TOKEN_B),
        )
    assert str(caught.value) == SESSION_CREDENTIAL_CONFLICT_MESSAGE


def _websocket_scope(*, query=None, bearer=None, cookie=None):
    headers = []
    if bearer is not None:
        headers.append((b'authorization', f'bEaReR {bearer}'.encode()))
    if cookie is not None:
        headers.append((b'cookie', f'other=x; session_key={cookie}'.encode()))
    return {
        'query_string': (
            f'token={query}'.encode() if query is not None else b''
        ),
        'headers': headers,
    }


@pytest.mark.parametrize(
    ('scope', 'expected_source'),
    [
        (_websocket_scope(query=TOKEN_A), 'query'),
        (_websocket_scope(bearer=TOKEN_A), 'bearer'),
        (
            _websocket_scope(
                query=TOKEN_A, bearer=TOKEN_A, cookie=TOKEN_A,
            ),
            'query+bearer+cookie',
        ),
    ],
)
def test_websocket_credential_sources_must_resolve_one_identity(
    scope, expected_source,
):
    token, source = resolve_websocket_session_credential(scope)
    assert token == TOKEN_A
    assert source == expected_source


@pytest.mark.parametrize(
    'scope',
    [
        _websocket_scope(query=TOKEN_A, bearer=TOKEN_B),
        _websocket_scope(query=TOKEN_A, cookie=TOKEN_B),
        {
            'query_string': f'token={TOKEN_A}&token={TOKEN_B}'.encode(),
            'headers': [],
        },
    ],
)
def test_websocket_conflicts_fail_without_leaking_tokens(scope):
    with pytest.raises(SessionCredentialConflict) as caught:
        resolve_websocket_session_credential(scope)
    rendered = f'{caught.value!r} {caught.value}'
    assert TOKEN_A not in rendered
    assert TOKEN_B not in rendered


def test_websocket_cookie_alone_does_not_expand_authentication_surface():
    assert resolve_websocket_session_credential(
        _websocket_scope(cookie=TOKEN_A),
    ) == (None, None)


def test_login_required_rejects_conflicting_credentials_before_view():
    called = False

    def protected(_request):
        nonlocal called
        called = True
        return JsonResponse({'success': True})

    response = login_required(protected)(
        _request(cookie=TOKEN_A, bearer=TOKEN_B),
    )
    body = json.loads(response.content)

    assert response.status_code == 401
    assert body == {
        'success': False,
        'message': SESSION_CREDENTIAL_CONFLICT_MESSAGE,
        'code': SESSION_CREDENTIAL_CONFLICT_CODE,
    }
    assert not called
    assert TOKEN_A not in response.content.decode()
    assert TOKEN_B not in response.content.decode()


def test_matching_dual_credentials_are_allowed_and_source_is_recorded():
    user = _user('dual-match@example.com')
    _session(user)

    def protected(request):
        return JsonResponse({
            'user_id': request.user.pk,
            'source': request.session_credential_source,
        })

    response = login_required(protected)(
        _request(cookie=TOKEN_A, bearer=TOKEN_A),
    )
    body = json.loads(response.content)

    assert response.status_code == 200
    assert body == {'user_id': user.pk, 'source': 'cookie+bearer'}


def test_role_gate_also_rejects_conflicting_credentials():
    response = admin_required(lambda _request: JsonResponse({'ok': True}))(
        _request(cookie=TOKEN_A, bearer=TOKEN_B),
    )
    assert response.status_code == 401
    assert json.loads(response.content)['code'] == SESSION_CREDENTIAL_CONFLICT_CODE


def _guard_response(request):
    return JsonResponse({'reached_login': True})


def _through_login_guard(request):
    return LoginTransitionGuardMiddleware(_guard_response)(request)


@pytest.mark.parametrize(
    'target_kind',
    ['user_id', 'email'],
)
@pytest.mark.parametrize('auth_mode', ['cookie', 'bearer', 'both'])
def test_authenticated_browser_must_logout_before_different_account(
    target_kind,
    auth_mode,
):
    current = _user('current@example.com')
    _session(current)
    target = (
        {'user_id': current.pk + 1000}
        if target_kind == 'user_id'
        else {'email': 'different@example.com'}
    )
    credentials = {
        'cookie': {'cookie': TOKEN_A},
        'bearer': {'bearer': TOKEN_A},
        'both': {'cookie': TOKEN_A, 'bearer': TOKEN_A},
    }[auth_mode]

    response = _through_login_guard(_request(
        method='post',
        path='/api/customers/auth-login',
        data=target,
        **credentials,
    ))
    body = json.loads(response.content)

    assert response.status_code == 409
    assert body == {
        'success': False,
        'message': ACCOUNT_SWITCH_REQUIRES_LOGOUT_MESSAGE,
        'code': ACCOUNT_SWITCH_REQUIRES_LOGOUT_CODE,
    }
    assert TOKEN_A not in response.content.decode()


@pytest.mark.parametrize('target_kind', ['user_id', 'email'])
def test_authenticated_browser_may_log_in_again_as_same_account(target_kind):
    current = _user('same@example.com')
    _session(current)
    target = (
        {'user_id': f'00{current.pk}'}
        if target_kind == 'user_id'
        else {'email': ' SAME@example.com '}
    )

    response = _through_login_guard(_request(
        cookie=TOKEN_A,
        method='post',
        path='/api/customers/auth-login',
        data=target,
    ))

    assert response.status_code == 200
    assert json.loads(response.content) == {'reached_login': True}


def test_root_pos_login_defers_account_switch_to_pin_verification():
    current = _user('current-pos@example.com')
    _session(current)

    response = _through_login_guard(_request(
        cookie=TOKEN_A,
        method='post',
        path='/auth-login',
        data={'user_id': current.pk + 1000},
    ))

    assert response.status_code == 200
    assert json.loads(response.content) == {'reached_login': True}


def test_expired_browser_session_does_not_block_fresh_login():
    current = _user('expired@example.com')
    _session(current, expired=True)

    response = _through_login_guard(_request(
        cookie=TOKEN_A,
        method='post',
        path='/api/customers/auth-login',
        data={'email': 'different@example.com'},
    ))

    assert response.status_code == 200


def test_login_guard_rejects_conflicting_dual_credentials():
    response = _through_login_guard(_request(
        cookie=TOKEN_A,
        bearer=TOKEN_B,
        method='post',
        path='/api/customers/auth-login',
        data={'email': 'somebody@example.com'},
    ))

    assert response.status_code == 401
    assert json.loads(response.content)['code'] == SESSION_CREDENTIAL_CONFLICT_CODE


def test_login_transition_guard_is_enabled_in_shared_settings():
    assert (
        'base.middlewares.login_transition_guard.LoginTransitionGuardMiddleware'
        in settings.MIDDLEWARE
    )
