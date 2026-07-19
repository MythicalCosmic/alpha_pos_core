from datetime import timedelta
import secrets

import pytest
from django.utils import timezone

from base.models import Session, User
from base.repositories.session import SessionRepository
from core.realtime.consumers import OrderQueueConsumer


pytestmark = pytest.mark.django_db


def _consumer_for(user, *, expires_at=None):
    raw = secrets.token_hex(32)
    session = Session.objects.create(
        user_id=user,
        ip_address='127.0.0.1',
        user_agent='realtime-test',
        payload=SessionRepository.hash_token(raw),
        expires_at=expires_at or timezone.now() + timedelta(hours=1),
    )
    consumer = OrderQueueConsumer()
    consumer.scope = {
        'query_string': f'token={raw}'.encode(),
        'headers': [],
    }
    return consumer, session, raw


def _user(role, suffix):
    return User.objects.create(
        first_name='Realtime',
        last_name=suffix,
        email=f'realtime-{suffix}@example.com',
        password='!',
        role=role,
        status=User.UserStatus.ACTIVE,
    )


def test_realtime_accepts_active_staff_session():
    user = _user(User.RoleChoices.CASHIER, 'cashier')
    consumer, _session, _raw = _consumer_for(user)

    resolved = consumer._session_user()

    assert resolved.pk == user.pk


@pytest.mark.parametrize('role', [User.RoleChoices.USER, User.RoleChoices.COURIER])
def test_realtime_rejects_non_staff_session(role):
    user = _user(role, role.lower())
    consumer, _session, _raw = _consumer_for(user)

    assert consumer._session_user() is None


def test_realtime_rejects_and_revokes_expired_session():
    user = _user(User.RoleChoices.CASHIER, 'expired')
    consumer, session, raw = _consumer_for(
        user,
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    # Prime the repository cache to prove cached rows do not extend lifetime.
    assert SessionRepository.get_by_session_key(raw) is not None

    assert consumer._session_user() is None
    assert not Session.objects.filter(pk=session.pk).exists()
    assert SessionRepository.get_by_session_key(raw) is None
