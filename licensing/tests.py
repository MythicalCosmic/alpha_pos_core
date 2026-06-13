"""Tests for the kill-switch middleware + URL allowlist + cache flow.

We exercise the middleware through the Django test client rather than
unit-testing it in isolation, because the position-of-MIDDLEWARE behavior
is the actual contract: a /healthz request must always pass, and a
business endpoint must 503 while UNREGISTERED.
"""
import pytest
from django.test import Client


pytestmark = pytest.mark.django_db


def _client():
    return Client()


def _unregister_license():
    """Reset the License row to UNREGISTERED, undoing conftest's
    autouse `_active_license` fixture for the duration of this test."""
    from licensing.models import License
    lic = License.load()
    lic.status = License.Status.UNREGISTERED
    lic.last_heartbeat_at = None
    lic.last_server_now = None
    lic.expires_at = None
    # Mirror a fresh-install row — the registration tests rely on these
    # fields being empty so they can assert what register() writes.
    lic.org_name = ''
    lic.email = ''
    lic.key_encrypted = b''
    lic.save()


class TestMiddlewareAllowlist:
    """Allowlisted paths must work even when the license is UNREGISTERED
    (the default state on a freshly-installed POS — there is no License
    row yet, so .load() will create one with status=UNREGISTERED)."""

    def test_healthz_passes_without_license(self):
        _unregister_license()
        resp = _client().get('/healthz')
        assert resp.status_code == 200
        # Body is "ok <commit>"; the stable contract is the leading "ok".
        assert resp.content.startswith(b'ok')

    def test_status_endpoint_passes_without_license(self):
        _unregister_license()
        resp = _client().get('/api/licensing/status')
        assert resp.status_code == 200
        body = resp.json()
        assert body['success'] is True
        assert body['data']['status'] == 'UNREGISTERED'
        assert body['data']['is_blocked'] is True
        assert body['data']['reason'] == 'license_unregistered'

    def test_setup_endpoint_reachable_without_license(self):
        # Setup is allowlisted in the kill switch — must not 503. A
        # POST with no JSON body validly returns 400 (bad request) from
        # the view itself; the point of this test is that the middleware
        # didn't refuse it first.
        _unregister_license()
        resp = _client().post('/api/licensing/setup')
        assert resp.status_code != 503
        assert resp.status_code in (400, 422)

class TestKillSwitch:
    """Non-allowlisted endpoints must 503 while UNREGISTERED, with a
    payload the client can switch on."""

    def test_business_endpoint_blocked_when_unregistered(self):
        _unregister_license()
        # Pick a representative business endpoint — anything under /api/
        # that isn't /api/licensing or /api/sync/health. The login view
        # accepts POSTs without auth, so it's a clean test target.
        resp = _client().post('/api/admins/auth-login')
        assert resp.status_code == 503
        body = resp.json()
        assert body['success'] is False
        assert body['code'] == 'license_unregistered'
        assert body['status'] == 'UNREGISTERED'
        assert 'message' in body

    def test_get_blocked_too(self):
        _unregister_license()
        resp = _client().get('/api/admins/dashboard/today')
        assert resp.status_code == 503

    def test_options_passes_so_cors_preflight_works(self):
        # If we 503'd preflight, the browser would never send the real
        # request and the renderer couldn't even see the kill-switch
        # body. corsheaders runs before us; we just no-op on OPTIONS.
        _unregister_license()
        resp = _client().options('/api/admins/dashboard/today')
        assert resp.status_code != 503

    def test_dev_bypass_lets_everything_through(self, settings):
        """LICENSE_DEV_BYPASS disables the kill switch entirely (dev only)."""
        _unregister_license()  # would normally 503
        settings.LICENSE_DEV_BYPASS = True
        resp = _client().get('/api/admins/dashboard/today')
        assert resp.status_code != 503

    def test_dev_bypass_off_still_blocks(self, settings):
        _unregister_license()
        settings.LICENSE_DEV_BYPASS = False
        resp = _client().get('/api/admins/dashboard/today')
        assert resp.status_code == 503


class TestStateTransitions:
    """The middleware reads state from cache; when the License row
    flips, cache must bust so the next request sees the new status."""

    def test_active_license_unblocks_endpoints(self):
        from licensing.models import License
        from licensing.services import state as state_mod
        from django.utils import timezone
        from datetime import timedelta

        lic = License.load()
        lic.status = License.Status.ACTIVE
        lic.last_heartbeat_at = timezone.now()
        lic.last_server_now = timezone.now()
        lic.expires_at = timezone.now() + timedelta(days=30)
        lic.org_name = 'Test Cafe'
        lic.email = 'owner@test.local'
        lic.save()

        # save() busts the cache; next request rebuilds and sees ACTIVE.
        snapshot = state_mod.get_state()
        assert snapshot.status == 'ACTIVE'
        assert snapshot.is_blocked() is False

        # No business endpoint should refuse now (this one returns 401 for
        # missing creds — that's the point: we got past licensing).
        resp = _client().get('/api/admins/dashboard/today')
        assert resp.status_code != 503

    def test_suspended_license_blocks_immediately(self):
        from licensing.models import License
        from django.utils import timezone

        lic = License.load()
        lic.status = License.Status.SUSPENDED
        lic.last_heartbeat_at = timezone.now()
        lic.last_server_now = timezone.now()
        lic.save()

        resp = _client().get('/api/admins/dashboard/today')
        assert resp.status_code == 503
        assert resp.json()['code'] == 'license_suspended'

    def test_offline_grace_exceeded_blocks(self):
        from licensing.models import License
        from django.utils import timezone
        from datetime import timedelta

        # Active status but last heartbeat is 10 days ago — beyond the
        # default 7-day grace window. Should block.
        lic = License.load()
        lic.status = License.Status.ACTIVE
        lic.last_heartbeat_at = timezone.now() - timedelta(days=10)
        lic.last_server_now = timezone.now() - timedelta(days=10)
        lic.save()

        resp = _client().get('/api/admins/dashboard/today')
        assert resp.status_code == 503
        assert resp.json()['code'] == 'license_offline_grace_exceeded'

    def test_grace_anchors_on_last_server_now(self, monkeypatch):
        """Grace check uses max(wall_clock, last_server_now). When the server
        has reported a time past grace_until — even if the host wall clock
        was rolled back — the install is blocked.

        Limit of this defense: in the normal heartbeat lifecycle
        last_server_now == last_heartbeat_at after every successful tick, so
        an operator who rolls the wall clock back to BEFORE the last heartbeat
        cannot be detected by this anchor alone. A proper rollback defense
        would require a monotonically-bumped wall-clock high-water mark
        (separate work). This test pins the anchor's CURRENT contract."""
        from datetime import datetime, timedelta, timezone as tzlib
        from django.utils import timezone

        from licensing.models import License
        from licensing.services import state as state_mod

        wall = timezone.now()
        # Pretend a heartbeat 10 days ago, then somehow last_server_now got
        # bumped much further ahead (e.g. a control-center admin override).
        # The wall clock has since been rolled back into the past.
        lic = License.load()
        lic.status = License.Status.ACTIVE
        lic.last_heartbeat_at = wall - timedelta(days=10)
        lic.last_server_now = wall + timedelta(days=30)  # server > grace_until
        lic.save()

        rolled_back = wall - timedelta(days=50)

        class _FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return rolled_back if tz is None else rolled_back.astimezone(tz)

        monkeypatch.setattr(state_mod, 'datetime', _FakeDT)

        snapshot = state_mod.build_from_license(License.load())
        assert snapshot.is_blocked() is True
        assert snapshot.reason_code() == 'license_offline_grace_exceeded'

class TestMiddlewarePositionAssertion:
    """If a future refactor moves the middleware out of its slot, boot
    must fail loudly. We exercise AppConfig.ready() by re-importing it
    under a modified settings.MIDDLEWARE."""

    def test_missing_middleware_raises(self, monkeypatch):
        from django.core.exceptions import ImproperlyConfigured
        from licensing.apps import LicensingConfig

        monkeypatch.setattr(
            'django.conf.settings.MIDDLEWARE',
            ['corsheaders.middleware.CorsMiddleware'],  # licensing absent
        )
        config = LicensingConfig.create('licensing')
        with pytest.raises(ImproperlyConfigured):
            config.ready()

    def test_middleware_before_cors_raises(self, monkeypatch):
        from django.core.exceptions import ImproperlyConfigured
        from licensing.apps import LicensingConfig

        monkeypatch.setattr(
            'django.conf.settings.MIDDLEWARE',
            [
                'licensing.middleware.LicenseEnforcementMiddleware',
                'corsheaders.middleware.CorsMiddleware',
            ],
        )
        config = LicensingConfig.create('licensing')
        with pytest.raises(ImproperlyConfigured):
            config.ready()


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for requests.Response used by the heartbeat client."""
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.text = ''
        self.headers = headers or {}
    def json(self):
        return self._body


# Bearer key the TestHeartbeatClient class encrypts into the License row in
# its _prep_active_with_key fixture. Centralised so the signature helper
# below stays in sync with what the test license actually carries.
_TEST_BEARER_KEY = 'live-license-key-aaaaaaaaaaaaaaaaaaaaa'


def _signed_response(status_code, body, *, key=_TEST_BEARER_KEY):
    """Build a _FakeResponse whose X-Response-Signature header is a valid
    HMAC-SHA256 of the canonical JSON body — what the real control center
    sends. Heartbeat tests use this on 200-paths; 401/410/5xx don't need a
    signature because do_heartbeat doesn't verify on those."""
    import json as _json
    import hmac
    import hashlib
    raw = _json.dumps(body, separators=(',', ':'), sort_keys=True).encode('utf-8')
    sig = hmac.new(key.encode('utf-8'), raw, hashlib.sha256).hexdigest()
    return _FakeResponse(
        status_code, body,
        headers={'X-Response-Signature': f'sha256={sig}'},
    )


class TestSetupWizard:
    """The setup wizard must (a) refuse anything but UNREGISTERED,
    (b) validate the payload, (c) relay control-center errors with the
    original status code, (d) on success persist the key encrypted and
    flip to ACTIVE."""

    def _setup(self, payload):
        return _client().post(
            '/api/licensing/setup',
            data=__import__('json').dumps(payload),
            content_type='application/json',
        )

    def test_refuses_when_already_active(self):
        # Conftest fixture leaves the License in ACTIVE state.
        resp = self._setup({'email': 'a@b.local'})
        assert resp.status_code == 409
        body = resp.json()
        assert body['code'] == 'already_registered'

    def test_missing_email_returns_422(self):
        _unregister_license()
        resp = self._setup({})
        assert resp.status_code == 422
        assert set(resp.json()['errors']) == {'email'}

    def test_invalid_json_returns_400(self):
        _unregister_license()
        resp = _client().post(
            '/api/licensing/setup', data='not json',
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_control_center_url_missing_returns_503(self, settings):
        _unregister_license()
        settings.LICENSE_CONTROL_CENTER_URL = ''
        resp = self._setup({'email': 'a@b.local'})
        assert resp.status_code == 503
        assert resp.json()['code'] == 'control_center_url_missing'

    def test_control_center_unreachable_returns_502(self, settings, monkeypatch):
        import requests
        _unregister_license()
        settings.LICENSE_CONTROL_CENTER_URL = 'https://does-not-exist.local'

        def _boom(*args, **kwargs):
            raise requests.ConnectionError('refused')

        monkeypatch.setattr('licensing.services.heartbeat.requests.post', _boom)
        resp = self._setup({'email': 'a@b.local'})
        assert resp.status_code == 502
        assert resp.json()['code'] == 'control_center_unreachable'

    def test_control_center_404_passes_through(self, settings, monkeypatch):
        _unregister_license()
        settings.LICENSE_CONTROL_CENTER_URL = 'https://cc.local'

        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _FakeResponse(404, {
                'success': False, 'message': 'Unknown email',
            }),
        )
        resp = self._setup({'email': 'unknown@x.local'})
        assert resp.status_code == 404

    def test_happy_path_activates_license_and_encrypts_key(
        self, settings, monkeypatch,
    ):
        from licensing.models import License
        from licensing.services import crypto
        from django.utils import timezone

        _unregister_license()
        settings.LICENSE_CONTROL_CENTER_URL = 'https://cc.local'

        captured = {}

        # `issued_at` must be NOW (not a hardcoded date) — the heartbeat
        # handler now anchors `last_heartbeat_at` on the server-reported
        # server_now/issued_at, so a stale fixture date trips the offline
        # grace window and the dashboard assertion below 503s.
        issued_at = timezone.now().isoformat()

        def _fake_post(url, json=None, **kw):
            captured['url'] = url
            captured['json'] = json
            return _FakeResponse(201, {
                'success': True,
                'key': 'fake-license-key-for-tests-' + 'x' * 40,
                'tenant_id': 7,
                'expires_at': None,
                'issued_at': issued_at,
            })

        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post', _fake_post,
        )

        resp = self._setup({'email': 'Owner@Plov.uz'})  # case folded
        assert resp.status_code == 201
        body = resp.json()
        assert body['success'] is True
        # CRITICAL: the wizard response NEVER includes the key.
        assert 'key' not in body
        assert 'license' in body
        assert body['license']['status'] == 'ACTIVE'

        # Wire format must contain ONLY email — no invite_code, no org_name.
        assert captured['json'] == {'email': 'owner@plov.uz'}

        # DB row should be ACTIVE, key encrypted (not cleartext), email
        # lowercased. org_name stays empty — set later via owner profile.
        lic = License.load()
        assert lic.status == 'ACTIVE'
        assert lic.email == 'owner@plov.uz'
        assert lic.org_name == ''
        assert lic.key_encrypted  # bytes blob present
        assert b'fake-license-key' not in bytes(lic.key_encrypted)
        # Roundtrip through Fernet returns the original cleartext.
        decrypted = crypto.decrypt_key(lic.key_encrypted)
        assert decrypted.startswith('fake-license-key')
        # last_heartbeat_at populated so the grace window starts now.
        assert lic.last_heartbeat_at is not None

        # And the kill switch should now LET requests through.
        resp2 = _client().get('/api/admins/dashboard/today')
        assert resp2.status_code != 503


class TestCryptoRoundtrip:
    def test_encrypt_decrypt_roundtrip(self):
        from licensing.services import crypto
        secret = 'super-secret-license-key-abcdef'
        blob = crypto.encrypt_key(secret)
        assert secret.encode() not in bytes(blob)
        assert crypto.decrypt_key(blob) == secret

    def test_empty_input(self):
        from licensing.services import crypto
        assert crypto.encrypt_key('') == b''
        assert crypto.decrypt_key(b'') is None

    def test_tampered_blob_returns_none(self):
        from licensing.services import crypto
        blob = crypto.encrypt_key('hello')
        assert crypto.decrypt_key(blob[:-1] + b'X') is None


# ---------------------------------------------------------------------------
# Heartbeat client
# ---------------------------------------------------------------------------


class TestHeartbeatClient:
    """Unit tests for `do_heartbeat` against a mocked control center.

    The real HTTP path is exercised end-to-end in the verification plan
    (both projects running side by side); here we just confirm each
    response code drives the License row the right way."""

    def _prep_active_with_key(self, settings):
        """Set up a License row that's ACTIVE with an encrypted key. The
        autouse fixture only sets ACTIVE without a real key, so heartbeats
        would fail decryption."""
        from licensing.models import License
        from licensing.services import crypto
        from django.utils import timezone
        from datetime import timedelta

        settings.LICENSE_CONTROL_CENTER_URL = 'https://cc.local'
        lic = License.load()
        lic.status = License.Status.ACTIVE
        lic.key_encrypted = crypto.encrypt_key(
            'live-license-key-aaaaaaaaaaaaaaaaaaaaa',
        )
        lic.email = 'demo@x.local'
        lic.org_name = 'Demo'
        lic.last_heartbeat_at = timezone.now() - timedelta(minutes=5)
        lic.expires_at = timezone.now() + timedelta(days=30)
        lic.save()
        return lic

    def test_unregistered_returns_304(self, settings):
        from licensing.services import heartbeat as hb
        _unregister_license()
        settings.LICENSE_CONTROL_CENTER_URL = 'https://cc.local'
        body, status = hb.do_heartbeat()
        assert status == 304

    def test_url_missing_returns_503(self, settings):
        from licensing.services import heartbeat as hb
        settings.LICENSE_CONTROL_CENTER_URL = ''
        body, status = hb.do_heartbeat()
        assert status == 503

    def test_network_failure_returns_502_no_state_change(
        self, settings, monkeypatch,
    ):
        import requests
        from licensing.models import License
        from licensing.services import heartbeat as hb

        lic_before = self._prep_active_with_key(settings)
        before_heartbeat = lic_before.last_heartbeat_at

        def _boom(*a, **kw):
            raise requests.ConnectionError('refused')
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post', _boom,
        )

        body, status = hb.do_heartbeat()
        assert status == 502
        # last_heartbeat_at NOT updated — grace must tick toward expiry.
        lic_after = License.load()
        assert lic_after.last_heartbeat_at == before_heartbeat
        assert lic_after.status == 'ACTIVE'

    def test_401_flips_to_suspended_immediately(self, settings, monkeypatch):
        from licensing.models import License
        from licensing.services import heartbeat as hb
        self._prep_active_with_key(settings)
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _FakeResponse(401, {'message': 'unknown'}),
        )
        body, status = hb.do_heartbeat()
        assert status == 401
        lic = License.load()
        assert lic.status == 'SUSPENDED'
        # Operator-visible explanation written to the banner field.
        assert 'rejected' in lic.last_message.lower() or 'revoked' in lic.last_message.lower()

    def test_410_flips_to_suspended_too(self, settings, monkeypatch):
        from licensing.models import License
        from licensing.services import heartbeat as hb
        self._prep_active_with_key(settings)
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _FakeResponse(410, {'message': 'revoked'}),
        )
        body, status = hb.do_heartbeat()
        assert status == 410
        assert License.load().status == 'SUSPENDED'

    def test_5xx_transient_does_not_change_state(self, settings, monkeypatch):
        from licensing.models import License
        from licensing.services import heartbeat as hb
        self._prep_active_with_key(settings)
        before_status = License.load().status
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _FakeResponse(502, {}),
        )
        body, status = hb.do_heartbeat()
        assert status == 503
        assert License.load().status == before_status

    def test_200_active_updates_state(self, settings, monkeypatch):
        from licensing.models import License
        from licensing.services import heartbeat as hb
        from django.utils import timezone
        from datetime import timedelta

        self._prep_active_with_key(settings)
        future_iso = (timezone.now() + timedelta(days=60)).isoformat()
        now_iso = timezone.now().isoformat()
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _signed_response(200, {
                'status': 'ACTIVE',
                'expires_at': future_iso,
                'server_now': now_iso,
                'next_heartbeat_in_s': 300,
                'message': 'Welcome',
                'ack_id': 'abc-123',
            }),
        )
        body, status = hb.do_heartbeat()
        assert status == 200
        lic = License.load()
        assert lic.status == 'ACTIVE'
        assert lic.last_message == 'Welcome'
        assert lic.last_heartbeat_at  # updated
        assert lic.expires_at is not None

    def test_200_suspended_flips_state_and_busts_cache(
        self, settings, monkeypatch,
    ):
        """Critical for enforcement: control center says SUSPENDED → the
        middleware must refuse the very next request, not wait 60s for
        the cache TTL."""
        from licensing.models import License
        from licensing.services import heartbeat as hb
        from django.utils import timezone

        self._prep_active_with_key(settings)
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _signed_response(200, {
                'status': 'SUSPENDED',
                'expires_at': None,
                'server_now': timezone.now().isoformat(),
                'message': 'Subscription overdue',
                'ack_id': 'x',
            }),
        )
        # Pre-heartbeat: request would pass.
        assert _client().get('/api/admins/dashboard/today').status_code != 503

        hb.do_heartbeat()

        # Post-heartbeat: middleware must refuse immediately.
        resp = _client().get('/api/admins/dashboard/today')
        assert resp.status_code == 503
        assert resp.json()['code'] == 'license_suspended'

    def test_unknown_status_preserves_current_does_not_revive(
        self, settings, monkeypatch,
    ):
        """Fail-CLOSED: an unknown status in the heartbeat response must not
        flip SUSPENDED back to ACTIVE. Previously the code coerced unknown
        values to ACTIVE — a malicious MITM or contract drift could have
        silently revived a killed install."""
        from licensing.models import License
        from licensing.services import heartbeat as hb
        from django.utils import timezone

        self._prep_active_with_key(settings)
        # Manually flip to SUSPENDED so we have something the bad response
        # would otherwise revive.
        lic = License.load()
        lic.status = License.Status.SUSPENDED
        lic.save()

        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _signed_response(200, {
                'status': 'TOTALLY_BOGUS',  # not a valid choice
                'expires_at': None,
                'server_now': timezone.now().isoformat(),
                'ack_id': 'x',
            }),
        )
        body, status = hb.do_heartbeat()
        assert status == 200
        # SUSPENDED must NOT have been coerced to ACTIVE.
        assert License.load().status == 'SUSPENDED'

    def test_401_does_not_bump_last_heartbeat_at(self, settings, monkeypatch):
        """A rejected heartbeat must not advance the grace clock — that
        would silently extend the offline-grace window if status
        enforcement ever gets softened."""
        from licensing.models import License
        from licensing.services import heartbeat as hb

        self._prep_active_with_key(settings)
        before = License.load().last_heartbeat_at
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _FakeResponse(401, {'message': 'unknown'}),
        )
        hb.do_heartbeat()
        assert License.load().last_heartbeat_at == before

    def test_undecryptable_key_fails_closed_to_suspended(self, settings, monkeypatch):
        """If the Fernet key rotated or the encrypted blob is corrupt, the
        daemon can't call /heartbeat at all. The install would otherwise
        drift ACTIVE until grace_until lapsed (days). Fail CLOSED: flip
        SUSPENDED so the middleware blocks on the next request."""
        from licensing.models import License, LicenseEvent
        from licensing.services import heartbeat as hb

        settings.LICENSE_CONTROL_CENTER_URL = 'https://cc.local'
        lic = License.load()
        lic.status = License.Status.ACTIVE
        lic.key_encrypted = b'corrupted-blob'
        lic.save()

        body, status = hb.do_heartbeat()
        assert status == 500
        assert body['message'] == 'license_key_undecryptable'

        lic_after = License.load()
        assert lic_after.status == 'SUSPENDED'
        assert 'decrypted' in lic_after.last_message.lower()

        # Audit row written.
        assert LicenseEvent.objects.filter(
            action=LicenseEvent.Action.STATUS_CHANGED,
            detail__reason='key_undecryptable',
        ).exists()

    def test_bad_response_signature_returns_502_no_state_change(
        self, settings, monkeypatch,
    ):
        """A 200 OK without a valid X-Response-Signature must be ignored.
        Defeats a MITM that has bypassed TLS and is trying to forge an
        ACTIVE response."""
        from licensing.models import License
        from licensing.services import heartbeat as hb
        from django.utils import timezone

        self._prep_active_with_key(settings)
        # Pretend the License got SUSPENDED — a forged response that
        # successfully revived it would be the attack we're blocking.
        lic = License.load()
        lic.status = License.Status.SUSPENDED
        lic.save()

        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            # No headers → no signature → must be rejected.
            lambda *a, **kw: _FakeResponse(200, {
                'status': 'ACTIVE',  # the lie we'd love to defeat
                'expires_at': None,
                'server_now': timezone.now().isoformat(),
                'ack_id': 'forged',
            }),
        )
        body, status = hb.do_heartbeat()
        assert status == 502
        assert body['message'] == 'response_signature_invalid'
        # The forged "ACTIVE" must NOT have stuck.
        assert License.load().status == 'SUSPENDED'

    def test_wrong_signature_returns_502(self, settings, monkeypatch):
        from licensing.models import License
        from licensing.services import heartbeat as hb
        from django.utils import timezone

        self._prep_active_with_key(settings)
        # Signature computed with the wrong key — should fail verification.
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _signed_response(200, {
                'status': 'ACTIVE',
                'expires_at': None,
                'server_now': timezone.now().isoformat(),
                'ack_id': 'tampered',
            }, key='wrong-key-not-the-bearer'),
        )
        body, status = hb.do_heartbeat()
        assert status == 502

    def test_last_heartbeat_at_anchored_to_server_now(self, settings, monkeypatch):
        """last_heartbeat_at must be the server's clock, not the local wall
        clock — otherwise winding the host clock forward, heartbeating, and
        winding back would silently extend grace_until."""
        from licensing.models import License
        from licensing.services import heartbeat as hb
        from django.utils.dateparse import parse_datetime

        self._prep_active_with_key(settings)
        server_now_iso = '2030-01-15T12:00:00+00:00'  # arbitrary future-looking
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post',
            lambda *a, **kw: _signed_response(200, {
                'status': 'ACTIVE',
                'expires_at': '2031-01-15T12:00:00+00:00',
                'server_now': server_now_iso,
                'ack_id': 'anchor',
            }),
        )
        hb.do_heartbeat()
        lic = License.load()
        # The DB value is timezone-aware; compare via parse_datetime.
        assert lic.last_heartbeat_at == parse_datetime(server_now_iso)
        assert lic.last_server_now == parse_datetime(server_now_iso)

    def test_https_required_in_production(self, settings, monkeypatch):
        """Non-HTTPS control center URL must be refused when DEBUG=False
        — silently downgrading would let an on-path attacker forge ACTIVE
        responses."""
        from licensing.services import heartbeat as hb

        self._prep_active_with_key(settings)
        settings.LICENSE_CONTROL_CENTER_URL = 'http://cc.local'  # plaintext
        settings.DEBUG = False  # production mode

        # requests.post must NOT be called — refusal happens earlier.
        def _should_not_be_called(*a, **kw):
            raise AssertionError('requests.post called despite plaintext URL')
        monkeypatch.setattr(
            'licensing.services.heartbeat.requests.post', _should_not_be_called,
        )

        body, status = hb.do_heartbeat()
        assert status == 503
        assert body['message'] == 'control_center_url_must_be_https'
