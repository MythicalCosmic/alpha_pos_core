"""Committed revocations must survive a concurrent authentication cache fill."""
from datetime import timedelta
from threading import Event, Thread

import pytest
from django.db import close_old_connections, connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from base.models import Session, User
from base.repositories.session import SessionRepository

pytestmark = pytest.mark.django_db(transaction=True)
TOKEN = 'transaction-cache-test-token'


def _session():
    user = User.objects.create(
        first_name='Cache', last_name='Test', email='cache-txn@test.local',
        password='!', role='ADMIN', status='ACTIVE',
    )
    session = Session.objects.create(
        user_id=user, payload=SessionRepository.hash_token(TOKEN),
        ip_address='127.0.0.1', user_agent='test',
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return user, session


def _revoke(user, session, operation):
    if operation == 'role':
        user.role = 'CASHIER'
        user.save(update_fields=['role'])
    elif operation == 'status':
        user.status = 'INACTIVE'
        user.save(update_fields=['status'])
    else:
        session.delete()


def _assert_revoked(operation):
    current = SessionRepository.get_by_session_key(TOKEN)
    if operation == 'delete':
        assert current is None
    elif operation == 'role':
        assert current.user_id.role == 'CASHIER'
    else:
        assert current.user_id.status == 'INACTIVE'


@pytest.mark.parametrize('operation', ['role', 'status', 'delete'])
def test_cache_refill_before_revoke_commit_does_not_restore_access(operation):
    if connection.vendor != 'postgresql':
        pytest.skip('requires PostgreSQL independent transaction snapshots')
    user, session = _session()
    assert SessionRepository.get_by_session_key(TOKEN) is not None
    changed, finish = Event(), Event()
    errors = []

    def writer():
        close_old_connections()
        try:
            with transaction.atomic():
                _revoke(user, session, operation)
                changed.set()
                assert finish.wait(10)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    thread = Thread(target=writer)
    thread.start()
    try:
        assert changed.wait(10)
        # PostgreSQL still exposes the old committed row. A normal auth
        # request refills the shared cache after the pre-commit eviction.
        old = SessionRepository.get_by_session_key(TOKEN)
        assert old is not None and old.user_id.role == 'ADMIN'
        assert old.user_id.status == 'ACTIVE'
    finally:
        finish.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    _assert_revoked(operation)


@pytest.mark.parametrize('operation', ['role', 'delete'])
def test_database_read_finishing_after_revoke_cannot_poison_future_auth(
    operation, monkeypatch,
):
    user, session = _session()
    read, finish = Event(), Event()
    errors = []
    original_first = QuerySet.first

    def hold_snapshot(qs):
        result = original_first(qs)
        if qs.model is Session and result is not None:
            read.set()
            assert finish.wait(10)
        return result

    def reader():
        close_old_connections()
        try:
            SessionRepository.get_by_session_key(TOKEN)
        except BaseException as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    monkeypatch.setattr(QuerySet, 'first', hold_snapshot)
    thread = Thread(target=reader)
    thread.start()
    try:
        assert read.wait(10)
        with transaction.atomic():
            _revoke(user, session, operation)
    finally:
        finish.set()
        thread.join(15)
        monkeypatch.setattr(QuerySet, 'first', original_first)
    assert not thread.is_alive() and not errors
    _assert_revoked(operation)


def test_saved_session_expiry_invalidates_previously_cached_expiry():
    _user, session = _session()
    assert not SessionRepository.get_by_session_key(TOKEN).is_expired()
    session.expires_at = timezone.now() - timedelta(seconds=1)
    session.save(update_fields=['expires_at'])
    assert SessionRepository.get_by_session_key(TOKEN).is_expired()


def test_auth_lookup_ignores_legacy_cache_and_uses_one_joined_query():
    from django.core.cache import cache
    from django.test.utils import CaptureQueriesContext

    user, session = _session()
    stale = Session.objects.select_related('user_id').get(pk=session.pk)
    _revoke(user, session, 'role')
    cache.set(f'session:{session.payload}', stale, 300)
    with CaptureQueriesContext(connection) as queries:
        current = SessionRepository.get_by_session_key(TOKEN)
        assert current.user_id.role == 'CASHIER'
    assert len(queries) == 1


def test_rolled_back_role_change_does_not_leak_a_cached_identity():
    user, _session_row = _session()
    with pytest.raises(RuntimeError, match='rollback'):
        with transaction.atomic():
            user.role = 'CASHIER'
            user.save(update_fields=['role'])
            assert SessionRepository.get_by_session_key(TOKEN).user_id.role == 'CASHIER'
            raise RuntimeError('rollback')
    assert SessionRepository.get_by_session_key(TOKEN).user_id.role == 'ADMIN'
