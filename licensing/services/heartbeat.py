"""Client-side talker to the pos_control_center.

This module owns the HTTP calls to /api/v1/register and /api/v1/heartbeat.
The heartbeat daemon (management command) imports `do_heartbeat`; the
setup wizard view imports `register`. Both return ServiceResponse-like
tuples (data, http_status) so views and the daemon stay thin.

Network failures are returned as errors, never raised — the caller
should be free to decide whether to surface them or queue a retry.

Tamper-proofing: every heartbeat response carries an HMAC signature
(``X-Response-Signature: sha256=<hex>``) keyed on the bearer license key.
A MITM with TLS interception can rewrite the JSON but can't forge the
signature without the key — see ``_verify_response_signature``. The
response is also anchored to the control center's ``server_now`` field
rather than the local wall clock, so a forward-skewed host clock can't
extend the offline grace window."""
import hashlib
import hmac
import json
import logging
import os
import platform
import socket
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from licensing.models import License, LicenseEvent
from licensing.services import crypto


logger = logging.getLogger(__name__)


def _verify_response_signature(body: Dict[str, Any], sig_header: str,
                                bearer_key: str) -> bool:
    """Return True iff the X-Response-Signature header matches an HMAC of the
    canonical-JSON body keyed on the bearer license key.

    Canonical form = ``json.dumps(body, separators=(',', ':'), sort_keys=True)``
    on both sides, so any pretty-printing the server framework adds (or
    proxies strip) does not break the signature."""
    if not sig_header or not sig_header.startswith('sha256='):
        return False
    expected_hex = sig_header[len('sha256='):].strip()
    raw = json.dumps(body, separators=(',', ':'), sort_keys=True).encode('utf-8')
    computed = hmac.new(
        bearer_key.encode('utf-8'), raw, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, expected_hex)


def _tls_verify_arg():
    """Return the value to pass as `verify=` to requests. A custom CA bundle
    is supported via LICENSE_TLS_CA_BUNDLE for private/internal CAs; otherwise
    we use the system trust store. NEVER disable verification — even in DEBUG
    the alpha_pos heartbeat goes over plain HTTP to localhost and `verify=True`
    is harmless there."""
    bundle = getattr(settings, 'LICENSE_TLS_CA_BUNDLE', '')
    return bundle if bundle else True


def _require_https(url: str) -> Optional[Tuple[Dict[str, Any], int]]:
    """Refuse to talk to a non-HTTPS control center URL in production. The
    settings.py boot check already guards against this for the static URL
    value, but this is a belt-and-braces check in case the env var is mutated
    at runtime — silently downgrading to plaintext would let any on-path
    attacker forge ACTIVE responses."""
    if getattr(settings, 'DEBUG', False):
        return None
    if not url.startswith('https://'):
        logger.error('heartbeat: refusing plaintext URL %s in production', url)
        return ({
            'success': False, 'message': 'control_center_url_must_be_https',
        }, 503)
    return None


def _http_timeout_s() -> int:
    """Heartbeat / register HTTP timeout — short enough that a hung control
    center doesn't tie up a worker, long enough for a normal round trip on
    a slow connection. Driven by LICENSE_HTTP_TIMEOUT_S so deployments on
    flaky links can raise it."""
    return getattr(settings, 'LICENSE_HTTP_TIMEOUT_S', 10)


def _persisted_install_id() -> str:
    """Last-resort machine id: a random UUID persisted to disk. Stable per
    install and unique, so the fingerprint never collapses to a value two
    installs can share (which is what `platform.node()` == hostname did)."""
    import uuid as _uuid
    base_dir = str(getattr(settings, 'BASE_DIR', '.'))
    path = getattr(settings, 'LICENSE_FINGERPRINT_FILE', '') or \
        os.path.join(base_dir, '.license_install_id')
    try:
        with open(path) as f:
            val = f.read().strip()
            if val:
                return val
    except OSError:
        pass
    val = _uuid.uuid4().hex
    try:
        with open(path, 'w') as f:
            f.write(val)
    except OSError:
        logger.warning('fingerprint: could not persist install id to %s', path)
    return val


def _machine_id() -> str:
    """Cross-platform stable machine identifier. /etc/machine-id is Linux-only;
    on Windows it does not exist, so the previous code fell back to
    platform.node() (== hostname) and every Windows install with the same
    computer name produced an identical fingerprint — defeating the control
    center's duplicate-install detection. Resolve per-OS instead."""
    # Linux / most Unix.
    for candidate in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            with open(candidate) as f:
                mid = f.read().strip()
                if mid:
                    return mid
        except OSError:
            continue
    # Windows: the per-install MachineGuid in the registry.
    if platform.system() == 'Windows':
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\Cryptography',
            ) as k:
                guid, _ = winreg.QueryValueEx(k, 'MachineGuid')
                if guid:
                    return str(guid)
        except OSError:
            pass
    # Final fallback: a persisted random UUID (never the bare hostname).
    return _persisted_install_id()


def _fingerprint() -> str:
    """sha256 of (hostname + machine-id). Stable across restarts of the
    same host; changes if the install is cloned to a new host. Used by the
    control center to flag duplicate installs (don't auto-block — surface
    only)."""
    import hashlib
    parts = [socket.gethostname(), _machine_id()]
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()


# Cached at module load: every heartbeat tick used to fork `git rev-parse`,
# which is wasted work in a Docker image without .git anyway. Resolved
# exactly once per process, falling back gracefully when git is absent.
_CLIENT_VERSION_CACHED: Optional[str] = None


def _client_version() -> str:
    """Short version string for the heartbeat payload. The control
    center records it per-event for support diagnostics."""
    global _CLIENT_VERSION_CACHED
    if _CLIENT_VERSION_CACHED is not None:
        return _CLIENT_VERSION_CACHED
    # PyInstaller releases do not contain a .git directory. The desktop
    # settings module injects its semantic version before this core module is
    # imported, giving support an authoritative installed release instead of
    # the historical alpha_pos@unknown value.
    semantic = str(os.environ.get('ALPHA_POS_CLIENT_VERSION') or '').strip()
    if semantic:
        _CLIENT_VERSION_CACHED = semantic[:100]
        return _CLIENT_VERSION_CACHED
    import subprocess
    try:
        sha = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        _CLIENT_VERSION_CACHED = f'alpha_pos@{sha}' if sha else 'alpha_pos@unknown'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _CLIENT_VERSION_CACHED = 'alpha_pos@unknown'
    return _CLIENT_VERSION_CACHED


def _control_url(path: str) -> str:
    base = (getattr(settings, 'LICENSE_CONTROL_CENTER_URL', '') or '').rstrip('/')
    return f'{base}/{path.lstrip("/")}'


def _parse_iso(value):
    if not value:
        return None
    if hasattr(value, 'isoformat'):
        return value
    return parse_datetime(value)


def _apply_heartbeat_response(lic: License, payload: Dict[str, Any],
                              expected_sent_at: Optional[str] = None) -> License:
    """Mutate the License singleton from a /heartbeat (or /register)
    response. Writes through the cache so the middleware sees the new
    state on the next request.

    `expected_sent_at` is the `sent_at` nonce this client put in the request.
    A live control center echoes it back inside the signed body, binding the
    signed response to *this* request — so a captured signed 200 cannot be
    replayed later (stale sent_at) or onto a clone (its request carries a
    different sent_at). If the server doesn't echo it yet we warn and fall
    back to the monotonic server_now guard alone."""
    if expected_sent_at is not None:
        echoed = payload.get('sent_at')
        if echoed is None:
            logger.warning(
                'heartbeat: response did not echo sent_at — replay binding '
                'unavailable; control center should echo the request nonce',
            )
        elif echoed != expected_sent_at:
            logger.error(
                'heartbeat: sent_at mismatch (echoed %r != sent %r); '
                'rejecting as replay', echoed, expected_sent_at,
            )
            LicenseEvent.objects.create(
                action=LicenseEvent.Action.HEARTBEAT_FAILED,
                detail={'kind': 'sent_at_mismatch'},
            )
            return lic

    valid_statuses = {c[0] for c in License.Status.choices}
    status_in = payload.get('status', License.Status.ACTIVE)
    if status_in not in valid_statuses:
        # Fail CLOSED on an unknown status: preserve whatever the License row
        # already held rather than coercing to ACTIVE. A bug or a malicious
        # MITM that drops in an unknown string must not silently revive a
        # SUSPENDED / EXPIRED install.
        logger.warning(
            'heartbeat: unknown status %r in response; preserving current %r',
            status_in, lic.status,
        )
        LicenseEvent.objects.create(
            action=LicenseEvent.Action.HEARTBEAT_FAILED,
            detail={'kind': 'unknown_status', 'received': str(status_in)[:40]},
        )
        status_in = lic.status

    # Anchor on the control center's clock, falling back to the local wall
    # clock only if the server didn't send one. This is critical for
    # `last_heartbeat_at` below: using `timezone.now()` lets an operator wind
    # the host clock to 2099, take a single heartbeat, wind it back, and
    # silently extend `grace_until` by decades. Sourcing from server_now kills
    # that trick.
    server_now = _parse_iso(payload.get('server_now')) or timezone.now()

    # Replay protection: refuse responses whose server clock is older than the
    # newest one already applied. Without this, a captured prior 200 could be
    # replayed to refresh last_heartbeat_at and extend the offline-grace window
    # indefinitely. server_now is monotonic for legitimate responses. Comparison
    # errors (naive/aware mismatch) fail toward applying — no worse than before.
    # `<=` (not `<`): a replay of the *most recent* response carries
    # server_now == last_server_now and must also be rejected, otherwise it
    # could keep refreshing last_heartbeat_at (and the grace window) forever.
    # Legitimate consecutive heartbeats always have a strictly greater
    # server_now (the control center's clock advances between beats).
    try:
        is_stale = bool(lic.last_server_now) and server_now <= lic.last_server_now
    except TypeError:
        is_stale = False
    if is_stale:
        logger.warning(
            'heartbeat: ignoring stale/replayed response (server_now %s < last %s)',
            server_now, lic.last_server_now,
        )
        LicenseEvent.objects.create(
            action=LicenseEvent.Action.HEARTBEAT_FAILED,
            detail={'kind': 'stale_server_now'},
        )
        return lic

    lic.status = status_in
    lic.expires_at = _parse_iso(payload.get('expires_at'))
    lic.last_message = payload.get('message') or ''

    # Display-only profile fields the control center may report. Captured only
    # when present so an older control center (or a heartbeat that omits them)
    # never blanks a value set by a previous response / the owner profile.
    org_in = payload.get('org_name')
    if org_in:
        lic.org_name = str(org_in)[:200]
    plan_in = payload.get('plan_name') or payload.get('plan')
    if plan_in:
        lic.plan_name = str(plan_in)[:100]
    # last_heartbeat_at is the anchor for the offline-grace clock; pin it to
    # the server's own `now` rather than the local wall clock so a tampered
    # host clock can't extend the grace window. Falls back to local time only
    # if the server omitted server_now (legacy / pre-signing).
    lic.last_heartbeat_at = server_now
    lic.last_server_now = server_now

    # Prepaid-billing snapshot (display-only). The control center sends
    # `balance` as a string, `days_remaining` as an int (or null), and `warn`
    # as a bool. Older control centers omit these — leave them None/False.
    balance_in = payload.get('balance')
    try:
        lic.balance = Decimal(str(balance_in)) if balance_in not in (None, '') else None
    except (InvalidOperation, ValueError):
        lic.balance = None
    days_in = payload.get('days_remaining')
    # bool is a subclass of int in Python — exclude it so a stray True/False
    # can't be coerced into a day count.
    lic.days_remaining = days_in if (isinstance(days_in, int) and not isinstance(days_in, bool)) else None
    lic.warn = bool(payload.get('warn', False))

    lic.save()
    # save() busts both license:row and license:state caches.
    return lic


# ---------------------------------------------------------------------------
# Setup wizard helper
# ---------------------------------------------------------------------------


def register(email: str, plan_id=None) -> Tuple[Dict[str, Any], int]:
    """POST /api/v1/register on the control center. On success, encrypt
    and persist the returned key + flip status to ACTIVE.

    Email is what the operator types; ``plan_id`` is the subscription plan
    they picked in the wizard's plan-picker step (if any). The control
    center looks up an invite the vendor pre-bound to that email, binds
    the chosen plan, and returns the bearer key. The key is encrypted at
    rest and never echoed back to the caller.

    Returns (body, http_status). The caller (the setup wizard view) just
    re-emits these.
    """
    url = _control_url('/api/v1/register')
    if not getattr(settings, 'LICENSE_CONTROL_CENTER_URL', ''):
        return ({
            'success': False,
            'message': 'LICENSE_CONTROL_CENTER_URL is not configured on this install.',
            'code': 'control_center_url_missing',
        }, 503)
    https_err = _require_https(url)
    if https_err is not None:
        return https_err

    payload = {'email': email}
    if plan_id is not None:
        payload['plan_id'] = plan_id
    LicenseEvent.objects.create(
        action=LicenseEvent.Action.SETUP_ATTEMPTED,
        detail={'email': email, 'plan_id': plan_id},
    )

    try:
        resp = requests.post(
            url, json=payload, timeout=_http_timeout_s(),
            verify=_tls_verify_arg(),
        )
    except requests.RequestException as exc:
        logger.exception('register: HTTP to control center failed')
        return ({
            'success': False,
            'message': f'Could not reach the control center: {exc}',
            'code': 'control_center_unreachable',
        }, 502)

    # Bubble the control center's error responses up unchanged so the
    # operator sees "email already used" / "invalid" / etc. with the
    # same status code the control center returned.
    if resp.status_code != 201:
        try:
            body = resp.json()
        except ValueError:
            body = {'success': False, 'message': resp.text[:500]}
        # Normalize the shape so the wizard caller always gets success+message.
        body.setdefault('success', False)
        body.setdefault('message', f'Control center returned {resp.status_code}')
        return body, resp.status_code

    body = resp.json()
    key = body.get('key') or ''
    if not key:
        return ({
            'success': False,
            'message': 'Control center returned a malformed response (no key).',
            'code': 'control_center_response_invalid',
        }, 502)

    # Verify the register response signature (same full-body HMAC convention as
    # the heartbeat), keyed on the returned key. This is weaker than the
    # heartbeat case — an active MITM that substitutes the key can re-sign with
    # it — so TLS (_require_https, enforced in production) stays the primary
    # defense. But a *present-but-invalid* signature means tampering or
    # corruption in flight: refuse to persist the key.
    #
    # A *present-but-invalid* signature always means tampering or corruption in
    # flight: refuse to persist the key. A *missing* signature is tolerated by
    # default (and only warned about), because TLS (_require_https, enforced in
    # production) is the primary defense and this signature is weaker than the
    # heartbeat's anyway — a MITM that substitutes the key can re-sign with it.
    # Operators whose control center signs /register can flip
    # LICENSE_REQUIRE_REGISTER_SIGNATURE=True to make a signature mandatory.
    # NB: keying mandatory-enforcement off DEBUG would break real activation,
    # since production builds (and the test runner) force DEBUG=False even
    # against a control center that doesn't sign /register.
    reg_sig = resp.headers.get('X-Response-Signature', '')
    if reg_sig:
        if not _verify_response_signature(body, reg_sig, key):
            logger.error('register: response signature failed verification')
            return ({
                'success': False,
                'message': 'Control center response failed signature verification.',
                'code': 'response_signature_invalid',
            }, 502)
    elif getattr(settings, 'LICENSE_REQUIRE_REGISTER_SIGNATURE', False):
        logger.error(
            'register: control center response is unsigned and '
            'LICENSE_REQUIRE_REGISTER_SIGNATURE is enabled; refusing the key.',
        )
        return ({
            'success': False,
            'message': 'Control center response was not signed.',
            'code': 'response_signature_missing',
        }, 502)
    else:
        logger.warning(
            'register: control center did not sign the response. TLS is the '
            'primary defense; set LICENSE_REQUIRE_REGISTER_SIGNATURE=True once '
            'the control center signs /register responses to enforce it.',
        )

    # TOCTOU close: recheck the singleton status INSIDE the row lock. The
    # view-level check happens outside any transaction, so two parallel setup
    # POSTs (different IPs, escaping the per-IP rate limit) could both pass
    # it; without this guard the second would clobber the first's encrypted
    # key with its own. select_for_update + the recheck makes the second one
    # error out cleanly.
    with transaction.atomic():
        lic = License.objects.select_for_update().get(pk=1)
        if lic.status != License.Status.UNREGISTERED:
            return ({
                'success': False,
                'code': 'already_registered',
                'message': f'This install is already in state {lic.status}.',
                'status': lic.status,
            }, 409)
        lic.key_encrypted = crypto.encrypt_key(key)
        lic.email = email
        # org_name is set later via the admin/owner-profile endpoint once
        # signup is done. Keep it empty (model default) at registration.
        lic.fingerprint = _fingerprint()
        lic.registered_at = timezone.now()
        # Treat the /register response shape as a heartbeat response.
        # _apply_heartbeat_response handles status / expires_at / server_now,
        # and captures org_name / plan_name when the control center supplies
        # them (it usually doesn't at register time — the owner profile sets
        # org_name later — so these stay blank unless echoed).
        _apply_heartbeat_response(lic, {
            'status': License.Status.ACTIVE,
            'expires_at': body.get('expires_at'),
            'server_now': body.get('issued_at') or timezone.now().isoformat(),
            'message': '',
            'org_name': body.get('org_name'),
            'plan_name': body.get('plan_name') or body.get('plan'),
        })

    LicenseEvent.objects.create(
        action=LicenseEvent.Action.SETUP_SUCCEEDED,
        detail={'email': email, 'tenant_id': body.get('tenant_id')},
    )

    return ({
        'success': True,
        'message': 'License activated.',
        'license': _sanitized_license(lic),
    }, 201)


def _sanitized_license(lic: License) -> Dict[str, Any]:
    """Public-safe snapshot of the License — never includes the key."""
    return {
        'status': lic.status,
        'org_name': lic.org_name,
        'email': lic.email,
        'expires_at': lic.expires_at.isoformat() if lic.expires_at else None,
        'registered_at': lic.registered_at.isoformat() if lic.registered_at else None,
        'last_heartbeat_at': (
            lic.last_heartbeat_at.isoformat() if lic.last_heartbeat_at else None
        ),
    }


# ---------------------------------------------------------------------------
# Heartbeat — periodic phone-home to confirm the license is still valid.
# ---------------------------------------------------------------------------


def do_heartbeat() -> Tuple[Dict[str, Any], int]:
    """Send one heartbeat to the control center. Returns (body, status)
    where status mirrors HTTP semantics:
      200  — success, License row updated, status applied.
      304  — no-op (UNREGISTERED — nothing to phone home about; the
             daemon should not count this as failure).
      401  — control center rejected our key (revoked / unknown). Local
             License is flipped to SUSPENDED with an explanatory message
             so the kill switch fires immediately, before grace.
      410  — same as 401 but explicitly "revoked"; same local effect.
      502  — network failure; License unchanged (grace ticks).
      503  — control center 5xx / transient; License unchanged.
    """
    if not getattr(settings, 'LICENSE_CONTROL_CENTER_URL', ''):
        return ({
            'success': False, 'message': 'control_center_url not configured',
        }, 503)

    lic = License.load()
    if lic.status == License.Status.UNREGISTERED:
        return ({'success': False, 'message': 'license unregistered'}, 304)

    cleartext = crypto.decrypt_key(lic.key_encrypted)
    if not cleartext:
        # LICENSE_FERNET_KEY rotated, or the stored blob is corrupt. The
        # daemon CANNOT call /heartbeat without the cleartext key, so the
        # install would otherwise drift along as ACTIVE indefinitely until
        # `grace_until` lapsed. Fail closed: flip status to SUSPENDED right
        # now so the middleware blocks on the next request. The operator
        # must re-run setup to recover.
        logger.error(
            'heartbeat: cannot decrypt stored license key — flipping to '
            'SUSPENDED. Operator must re-run setup wizard.',
        )
        with transaction.atomic():
            lic = License.objects.select_for_update().get(pk=1)
            if lic.status != License.Status.SUSPENDED:
                prior = lic.status
                lic.status = License.Status.SUSPENDED
                lic.last_message = (
                    'License key cannot be decrypted on this install. '
                    'Re-run the setup wizard to restore service.'
                )
                lic.save()
                LicenseEvent.objects.create(
                    action=LicenseEvent.Action.STATUS_CHANGED,
                    detail={'from': prior, 'to': 'SUSPENDED',
                            'reason': 'key_undecryptable'},
                )
        return ({
            'success': False, 'message': 'license_key_undecryptable',
        }, 500)

    url = _control_url('/api/v1/heartbeat')
    https_err = _require_https(url)
    if https_err is not None:
        return https_err

    sent_at = timezone.now().isoformat()
    payload = {
        'client_version': _client_version(),
        'branch_id': getattr(settings, 'BRANCH_ID', 'main'),
        'fingerprint': _fingerprint(),
        'sent_at': sent_at,
        'metrics': _collect_metrics(),
    }
    headers = {'Authorization': f'Bearer {cleartext}'}

    try:
        resp = requests.post(
            url, json=payload, headers=headers,
            timeout=_http_timeout_s(), verify=_tls_verify_arg(),
        )
    except requests.RequestException as exc:
        # Network failure: do NOT update last_heartbeat_at so grace
        # continues to count down. Logged at WARNING — common in normal
        # operations (brief internet outage); INFO would be noisy.
        logger.warning('heartbeat: network failure: %s', exc)
        LicenseEvent.objects.create(
            action=LicenseEvent.Action.HEARTBEAT_FAILED,
            detail={'kind': 'network', 'error': str(exc)[:200]},
        )
        return ({'success': False, 'message': str(exc)}, 502)

    if resp.status_code in (401, 410):
        # Control center says our key is bad. Flip local status so the
        # kill switch fires immediately rather than waiting for the
        # full offline-grace window. The exact reason (REVOKED vs bad
        # key) doesn't matter to enforcement — both block.
        #
        # Do NOT bump last_heartbeat_at / last_server_now here: a rejected
        # heartbeat is not a successful one. Leaving the timestamps alone
        # keeps the grace clock honest in case status enforcement ever gets
        # softened.
        with transaction.atomic():
            lic = License.objects.select_for_update().get(pk=1)
            lic.status = License.Status.SUSPENDED
            lic.last_message = (
                'Control center rejected this license key. Contact your '
                'POS vendor — the key may have been revoked.'
            )
            lic.save()
        LicenseEvent.objects.create(
            action=LicenseEvent.Action.STATUS_CHANGED,
            detail={'from': 'ACTIVE', 'to': 'SUSPENDED',
                    'reason': f'control_center_status_{resp.status_code}'},
        )
        return ({'success': False, 'message': 'rejected by control center',
                 'status_code': resp.status_code}, resp.status_code)

    if resp.status_code >= 500:
        # 5xx is transient; don't update last_heartbeat_at, let grace tick.
        logger.warning('heartbeat: control center 5xx %s', resp.status_code)
        LicenseEvent.objects.create(
            action=LicenseEvent.Action.HEARTBEAT_FAILED,
            detail={'kind': 'http_5xx', 'status_code': resp.status_code},
        )
        return ({'success': False, 'message': 'control center error',
                 'status_code': resp.status_code}, 503)

    if resp.status_code != 200:
        # Unexpected status (e.g. 4xx other than 401/410). Surface but
        # don't change local state. This catches contract drift.
        logger.warning('heartbeat: unexpected status %s', resp.status_code)
        return ({'success': False, 'message': 'unexpected status',
                 'status_code': resp.status_code}, resp.status_code)

    try:
        body = resp.json()
    except ValueError:
        return ({'success': False, 'message': 'invalid response body'}, 502)

    # Verify the HMAC signature BEFORE applying anything. A 200 without a
    # valid X-Response-Signature is treated the same as a 5xx — we leave the
    # License row alone so the grace clock keeps ticking. A MITM that has
    # stripped TLS would be able to forge any JSON body but not this header.
    sig_header = resp.headers.get('X-Response-Signature', '')
    if not _verify_response_signature(body, sig_header, cleartext):
        logger.error('heartbeat: response signature failed verification')
        LicenseEvent.objects.create(
            action=LicenseEvent.Action.HEARTBEAT_FAILED,
            detail={'kind': 'bad_signature',
                    'sig_present': bool(sig_header)},
        )
        return ({
            'success': False, 'message': 'response_signature_invalid',
        }, 502)

    # Success path: apply the response to the License row + bust cache.
    # The cache bust here is what makes the suspend → enforce gap as
    # short as one heartbeat (5 min default) rather than the 60s cache
    # TTL window.
    with transaction.atomic():
        lic = License.objects.select_for_update().get(pk=1)
        prior_status = lic.status
        _apply_heartbeat_response(lic, body, expected_sent_at=sent_at)
        if lic.status != prior_status:
            LicenseEvent.objects.create(
                action=LicenseEvent.Action.STATUS_CHANGED,
                detail={'from': prior_status, 'to': lic.status,
                        'ack_id': body.get('ack_id')},
            )

    LicenseEvent.objects.create(
        action=LicenseEvent.Action.HEARTBEAT_OK,
        detail={'status': lic.status, 'ack_id': body.get('ack_id')},
    )
    return body, 200


def _collect_metrics() -> Dict[str, Any]:
    """Tiny diagnostic payload for the control-center support view.
    Bounded on purpose — never include PII or order content."""
    # We intentionally don't import models at module load to keep startup
    # cheap. Import inside the function so a stock daemon process doesn't
    # eagerly initialise base.models.
    from django.db import DatabaseError
    metrics = {}
    try:
        from base.models import Order
        from django.utils import timezone as tz
        from datetime import timedelta
        cutoff = tz.now() - timedelta(hours=24)
        metrics['orders_24h'] = Order.objects.filter(
            created_at__gte=cutoff, is_deleted=False,
        ).count()
    except (ImportError, DatabaseError):
        # Schema may not exist (fresh install, mid-migration) or base hasn't
        # loaded yet — skip rather than crash the heartbeat. Anything wider
        # would hide real bugs in the metrics path.
        logger.debug('heartbeat: metrics collection skipped', exc_info=True)
    return metrics


# ---------------------------------------------------------------------------
# Plan catalog + plan-change request — wizard / settings UI helpers
# ---------------------------------------------------------------------------


def list_plans() -> Tuple[Dict[str, Any], int]:
    """GET /api/v1/plans on the control center. Used by the setup wizard
    (before any License key exists) and by the settings screen (to render
    the plan picker for a plan-change request). Returns the body / status
    verbatim so the renderer sees the same shape the control center
    publishes; this stops the alpha_pos side from needing a redeploy when
    the control center adds a field to plans."""
    url = _control_url('/api/v1/plans')
    if not getattr(settings, 'LICENSE_CONTROL_CENTER_URL', ''):
        return ({
            'success': False,
            'message': 'control_center_url_missing',
        }, 503)
    https_err = _require_https(url)
    if https_err is not None:
        return https_err
    try:
        resp = requests.get(
            url, timeout=_http_timeout_s(), verify=_tls_verify_arg(),
        )
    except requests.RequestException as exc:
        logger.warning('list_plans: network failure: %s', exc)
        return ({'success': False, 'message': 'control_center_unreachable'}, 502)
    try:
        body = resp.json()
    except ValueError:
        return ({'success': False, 'message': 'invalid_response_body'}, 502)
    return body, resp.status_code


def request_plan_change(plan_id, note: str = '') -> Tuple[Dict[str, Any], int]:
    """POST /api/v1/plan-change with the bearer key. Used by the settings
    screen when the operator picks a new plan and submits. The control
    center queues the request for vendor approval — billing is NOT touched
    until approval. The next heartbeat surfaces the pending request to the
    renderer.

    Returns (body, status) verbatim; success has status 201 (fresh) or
    200 (already-pending), failures bubble through unchanged."""
    url = _control_url('/api/v1/plan-change')
    if not getattr(settings, 'LICENSE_CONTROL_CENTER_URL', ''):
        return ({
            'success': False, 'message': 'control_center_url_missing',
        }, 503)
    https_err = _require_https(url)
    if https_err is not None:
        return https_err

    lic = License.load()
    if lic.status == License.Status.UNREGISTERED:
        return ({
            'success': False,
            'message': 'This install is not registered yet.',
            'code': 'unregistered',
        }, 409)
    cleartext = crypto.decrypt_key(lic.key_encrypted)
    if not cleartext:
        return ({
            'success': False, 'message': 'license_key_undecryptable',
        }, 500)

    try:
        resp = requests.post(
            url,
            json={'plan_id': plan_id, 'note': note},
            headers={'Authorization': f'Bearer {cleartext}'},
            timeout=_http_timeout_s(),
            verify=_tls_verify_arg(),
        )
    except requests.RequestException as exc:
        logger.warning('plan_change: network failure: %s', exc)
        return ({'success': False, 'message': str(exc)}, 502)
    try:
        body = resp.json()
    except ValueError:
        return ({'success': False, 'message': 'invalid_response_body'}, 502)
    return body, resp.status_code
