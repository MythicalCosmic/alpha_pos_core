"""POS device presence registry (WS Phase 2): the cloud records a till's
heartbeat from its sync headers, and resolve_active_cashier() returns the active
cashier on a CONNECTED till (verified against a synced ACTIVE shift), or None."""
from datetime import timedelta

import pytest
from django.utils import timezone

from base.services import presence


@pytest.fixture(autouse=True)
def _clear(_clear_caches):
    # presence lives in the default cache; conftest._clear_caches already wipes it.
    yield


def _cashier(email):
    from base.models import User
    return User.objects.create(first_name='Ca', last_name='Sh', email=email,
                               role='CASHIER', status='ACTIVE', password='!')


def _shift(user, branch_id='branch-1', status='ACTIVE', device_id=''):
    from base.models import Shift
    return Shift.objects.create(user=user, start_time=timezone.now(), status=status,
                                branch_id=branch_id, device_id=device_id)


@pytest.mark.django_db
class TestPresence:
    def test_mark_and_live(self):
        presence.mark_device_live('dev-A', 'branch-1', 7)
        live = presence.live_devices()
        assert len(live) == 1 and live[0]['device_id'] == 'dev-A'
        assert live[0]['cashier_id'] == 7 and live[0]['branch_id'] == 'branch-1'

    def test_no_device_id_is_noop(self):
        presence.mark_device_live('', 'branch-1', 7)
        assert presence.live_devices() == []

    def test_resolve_returns_connected_cashier_with_active_shift(self):
        u = _cashier('r1@x.local')
        _shift(u)
        presence.mark_device_live('dev-A', 'branch-1', u.id)
        res = presence.resolve_active_cashier()
        assert res is not None
        assert res['cashier_id'] == u.id and res['device_id'] == 'dev-A'

    def test_resolve_none_when_no_device_online(self):
        u = _cashier('r2@x.local')
        _shift(u)                       # cashier on shift but NO till heartbeat
        assert presence.resolve_active_cashier() is None

    def test_resolve_skips_cashier_without_active_shift(self):
        u = _cashier('r3@x.local')
        _shift(u, status='ENDED')       # shift closed -> not dispatch-ready
        presence.mark_device_live('dev-A', 'branch-1', u.id)
        assert presence.resolve_active_cashier() is None

    def test_resolve_branch_filter(self):
        u = _cashier('r4@x.local')
        _shift(u, branch_id='branch-1')
        presence.mark_device_live('dev-A', 'branch-1', u.id)
        assert presence.resolve_active_cashier(branch_id='branch-2') is None
        assert presence.resolve_active_cashier(branch_id='branch-1') is not None

    def test_resolve_prefers_most_recent_device(self):
        u1 = _cashier('r5a@x.local')
        _shift(u1)
        u2 = _cashier('r5b@x.local')
        _shift(u2)
        presence.mark_device_live('dev-OLD', 'branch-1', u1.id)
        presence.mark_device_live('dev-NEW', 'branch-1', u2.id)   # marked later -> newer ts
        res = presence.resolve_active_cashier()
        assert res['device_id'] == 'dev-NEW' and res['cashier_id'] == u2.id

    def test_resolve_stable_cashier_uuid_instead_of_foreign_local_pk(self):
        u = _cashier('uuid-ref@x.local')
        _shift(u)
        presence.mark_device_live('dev-UUID', 'branch-1', str(u.uuid))

        res = presence.resolve_active_cashier()

        assert res is not None
        assert res['cashier_id'] == u.id

    def test_device_branch_cannot_resolve_users_shift_on_another_branch(self):
        u = _cashier('wrong-branch@x.local')
        _shift(u, branch_id='branch-2')
        presence.mark_device_live('dev-A', 'branch-1', str(u.uuid))

        assert presence.resolve_active_cashier(branch_id='branch-1') is None

    def test_blank_legacy_device_branch_still_obeys_requested_branch(self):
        u = _cashier('blank-entry-branch@x.local')
        _shift(u, branch_id='branch-2')
        presence.mark_device_live('dev-LEGACY', '', str(u.uuid))

        assert presence.resolve_active_cashier(branch_id='branch-1') is None

    def test_bound_shift_cannot_be_claimed_by_another_devices_heartbeat(self):
        u = _cashier('bound-device@x.local')
        _shift(u, device_id='dev-A')
        presence.mark_device_live('dev-B', 'branch-1', str(u.uuid))

        assert presence.resolve_active_cashier(branch_id='branch-1') is None


@pytest.mark.django_db
class TestPresenceHeaders:
    def test_no_device_id_setting_yields_empty(self, settings):
        settings.DEVICE_ID = ''
        assert presence.device_presence_headers() == {}

    def test_headers_include_device_and_active_cashier(self, settings):
        settings.DEVICE_ID = 'dev-Z'
        settings.BRANCH_ID = 'branch-1'
        u = _cashier('h1@x.local')
        _shift(u)
        headers = presence.device_presence_headers()
        assert headers['X-Device-Id'] == 'dev-Z'
        assert headers['X-Active-Cashier'] == str(u.uuid)

    def test_header_ignores_newer_active_shift_from_another_branch(self, settings):
        settings.DEVICE_ID = 'dev-Z'
        settings.BRANCH_ID = 'branch-1'
        own = _cashier('own-branch@x.local')
        other = _cashier('other-branch@x.local')
        _shift(own, branch_id='branch-1')
        _shift(other, branch_id='branch-2')

        headers = presence.device_presence_headers()

        assert headers['X-Active-Cashier'] == str(own.uuid)

    def test_header_uses_owned_cashier_and_ignores_non_cashier_shift(self, settings):
        from base.models import Shift, User

        settings.DEVICE_ID = 'dev-Z'
        settings.BRANCH_ID = 'branch-1'
        cashier = _cashier('owned-cashier@x.local')
        manager = User.objects.create(
            first_name='Ma', last_name='Nager', email='manager@x.local',
            role='MANAGER', status='ACTIVE', password='!',
        )
        _shift(cashier, device_id='dev-Z')
        Shift.objects.create(
            user=manager, start_time=timezone.now() + timedelta(seconds=1),
            status='ACTIVE', branch_id='branch-1', device_id='',
        )

        headers = presence.device_presence_headers()

        assert headers['X-Active-Cashier'] == str(cashier.uuid)

    def test_header_does_not_advertise_newer_cashier_from_another_till(self, settings):
        from base.models import Shift

        settings.DEVICE_ID = 'dev-Z'
        settings.BRANCH_ID = 'branch-1'
        own = _cashier('owned-till@x.local')
        other = _cashier('other-till@x.local')
        _shift(own, device_id='dev-Z')
        Shift.objects.create(
            user=other, start_time=timezone.now() + timedelta(seconds=1),
            status='ACTIVE', branch_id='branch-1', device_id='dev-Y',
        )

        headers = presence.device_presence_headers()

        assert headers['X-Active-Cashier'] == str(own.uuid)
