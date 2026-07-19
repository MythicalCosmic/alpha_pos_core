from datetime import timedelta
import secrets

import pytest
from django.http import JsonResponse
from django.test import RequestFactory
from django.utils import timezone

from base.models import Session, User
from base.repositories.session import SessionRepository
from base.security.auth import login_required
from base.security.permissions import pos_staff_required


pytestmark = pytest.mark.django_db


def _courier_session():
    user = User.objects.create(
        first_name='Mobile',
        last_name='Courier',
        email='mobile-courier@example.com',
        password='!',
        role=User.RoleChoices.COURIER,
        status=User.UserStatus.ACTIVE,
    )
    raw = secrets.token_hex(32)
    Session.objects.create(
        user_id=user,
        payload=SessionRepository.hash_token(raw),
        ip_address='127.0.0.1',
        user_agent='courier-test',
        expires_at=timezone.now() + timedelta(hours=1),
    )
    request = RequestFactory().get(
        '/', HTTP_AUTHORIZATION=f'Bearer {raw}',
        HTTP_USER_AGENT='courier-test',
    )
    return request


def _ok(_request):
    return JsonResponse({'ok': True})


def test_courier_session_is_rejected_by_generic_login_gate():
    response = login_required(_ok)(_courier_session())
    assert response.status_code == 403


def test_courier_session_is_rejected_by_pos_staff_gate():
    response = pos_staff_required(_ok)(_courier_session())
    assert response.status_code == 403
