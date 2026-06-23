"""POS device presence registry — heartbeat over the existing cloud sync.

A till has no live socket to the cloud. Instead every cloud sync request it makes
(push ``/receive``, pull ``/changes``) carries ``X-Device-Id`` + ``X-Branch-ID`` +
``X-Active-Cashier`` headers (see ``device_presence_headers`` below, wired into
``sync.transport._auth_headers``). The cloud records each as a short-TTL presence
entry. ``resolve_active_cashier()`` then answers "which on-shift cashier is on a
CONNECTED till right now" — the link smartfood auto-dispatch needs to assign a
bot/delivery order to the active cashier of a LIVE POS, instead of any on-duty
cashier cloud-wide.

Backed by the default cache (Redis on the cloud) and ephemeral by design: a till
that stops syncing falls out of the registry when its TTL lapses, so a missed
disconnect can never strand a stale "connected" device. Multi-till safe — keyed
by device id, not branch id, so several tills on one branch token stay distinct.
"""
import logging
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger('base.presence')

# A till syncs every SYNC_INTERVAL (~30s); allow ~3 missed cycles before offline.
_TTL_SECONDS = 95
_INDEX_KEY = 'presence:devices'
_ENTRY_PREFIX = 'presence:device:'


def _entry_key(device_id):
    return f'{_ENTRY_PREFIX}{device_id}'


def mark_device_live(device_id, branch_id=None, cashier_id=None):
    """Cloud-side: record / refresh a till's presence from its sync headers.
    No-op without a device_id (older tills that don't send the header)."""
    if not device_id:
        return
    try:
        cashier_id = int(cashier_id) if cashier_id not in (None, '', 'None') else None
    except (TypeError, ValueError):
        cashier_id = None
    now = time.time()                      # float: sub-second 'most recent' tiebreak
    entry = {
        'device_id': str(device_id),
        'branch_id': str(branch_id or ''),
        'cashier_id': cashier_id,
        'ts': now,
    }
    try:
        cache.set(_entry_key(device_id), entry, _TTL_SECONDS)
        # Index the device id so resolve() can enumerate live devices without a
        # Redis SCAN (LocMemCache has no key iteration either). Prune stale ids so
        # the index can't grow without bound. Best-effort get-modify-set: a lost
        # update self-heals on the next ~30s sync.
        index = cache.get(_INDEX_KEY) or {}
        index[str(device_id)] = now
        cutoff = now - _TTL_SECONDS
        index = {d: t for d, t in index.items() if t >= cutoff}
        cache.set(_INDEX_KEY, index, _TTL_SECONDS * 4)
    except Exception:  # noqa: BLE001 — presence is best-effort, never fatal
        logger.debug('presence mark failed (device=%s)', device_id, exc_info=True)


def live_devices():
    """All currently-live presence entries (TTL not lapsed), most-recent first."""
    try:
        index = cache.get(_INDEX_KEY) or {}
    except Exception:
        return []
    out = []
    for device_id in list(index):
        entry = cache.get(_entry_key(device_id))
        if entry:
            out.append(entry)
    out.sort(key=lambda e: e.get('ts', 0), reverse=True)
    return out


def resolve_active_cashier(branch_id=None):
    """The active cashier on a CONNECTED till, or None when no POS is online / no
    on-shift cashier is present (Phase 3 rejects the order in that case).

    Verifies the reported cashier still has an ACTIVE shift on the cloud (the
    shift synced up) so a stale header can't assign a logged-out cashier — and
    dispatch re-checks the shift anyway. ``branch_id``, when given, restricts to
    that branch; otherwise any live till qualifies (single-restaurant default).
    Iterates live devices most-recent-first (the multi-till tiebreak)."""
    from base.models import Shift
    for entry in live_devices():
        if branch_id and entry.get('branch_id') and entry['branch_id'] != str(branch_id):
            continue
        cid = entry.get('cashier_id')
        if not cid:
            continue
        shift = (Shift.objects.filter(user_id=cid, status='ACTIVE', is_deleted=False)
                 .order_by('-start_time').first())
        if shift:
            return {
                'cashier_id': cid,
                'branch_id': entry.get('branch_id') or (shift.branch_id or ''),
                'device_id': entry.get('device_id'),
                'shift_id': shift.id,
            }
    return None


def device_presence_headers():
    """Till-side: presence headers describing THIS device for a cloud sync request.
    Returns {} on a non-till (no DEVICE_ID set). The active cashier is this till's
    most-recently-started open shift (best-effort, never breaks the sync)."""
    device_id = getattr(settings, 'DEVICE_ID', '') or ''
    if not device_id:
        return {}
    headers = {'X-Device-Id': str(device_id)}
    try:
        from base.models import Shift
        shift = (Shift.objects.filter(status='ACTIVE', is_deleted=False)
                 .order_by('-start_time').first())
        if shift:
            headers['X-Active-Cashier'] = str(shift.user_id)
    except Exception:  # noqa: BLE001 — never break a sync over presence
        logger.debug('active-cashier header lookup failed', exc_info=True)
    return headers
