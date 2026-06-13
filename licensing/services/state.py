"""In-memory snapshot of license state read by the middleware on every request.

Sits in the cache (key `license:state:v1`) so the hot-path check is one
cache hit, not a DB query. The heartbeat daemon writes here on every
response. The model `License.save()` busts this key too, so any DB-side
mutation (admin, shell, management command) takes effect on the next
request.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from django.conf import settings
from django.core.cache import cache


CACHE_KEY = 'license:state:v1'


def _cache_ttl() -> int:
    """TTL for the cached LicenseState. Dynamic so an operator can tune it
    without a code change. Defaults to 60s — short enough that an admin-side
    flip propagates fast even when the heartbeat daemon hasn't fired yet."""
    return getattr(settings, 'LICENSE_STATE_CACHE_TTL', 60)


@dataclass
class LicenseState:
    status: str  # License.Status value
    expires_at: Optional[str]  # ISO8601 or None
    last_heartbeat_at: Optional[str]
    last_server_now: Optional[str]
    grace_until: Optional[str]
    message: str
    org_name: str
    email: str
    # Prepaid-billing snapshot for the renderer (display-only). balance is a
    # string so it round-trips through the cache + JSON cleanly.
    balance: Optional[str] = None
    days_remaining: Optional[int] = None
    warn: bool = False

    def is_blocked(self) -> bool:
        """True if the middleware must refuse the request."""
        # No setup wizard run yet: refuse everything except the allowlist.
        if self.status == 'UNREGISTERED':
            return True

        # Control center explicitly suspended or marked expired.
        if self.status in ('SUSPENDED', 'EXPIRED'):
            return True

        # Self-enforce the signed expiry date. Previously expiry relied 100% on
        # the control center actively pushing status=EXPIRED; if that push never
        # arrived (control center blocked/MITM'd) an ACTIVE install ran forever.
        if self.expires_at and self._past_expiry():
            return True

        # Active but offline grace exhausted (no heartbeat for N days).
        # `grace_until` is computed by the heartbeat daemon as
        # last_heartbeat_at + LICENSE_GRACE_DAYS. If we've blown past it,
        # we treat the install as expired even though the last cached
        # status was active.
        if self.grace_until:
            try:
                until = datetime.fromisoformat(self.grace_until)
            except ValueError:
                # Fail CLOSED on a malformed grace marker. A corrupted cache
                # entry or unexpected serializer change must not silently
                # disable the kill switch.
                return True
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            # Use the conservative clock — see _now_anchored. Anchoring on
            # last_server_now defeats the "wind the host clock back" trick
            # that would otherwise extend grace indefinitely.
            if self._now_anchored() > until:
                return True

        return False

    def _now_anchored(self) -> datetime:
        """Return the later of (host wall clock, last trusted server time).
        Lets us survive an operator winding the local clock backward to dodge
        either the expiry check or the offline-grace check."""
        now_ref = datetime.now(timezone.utc)
        if self.last_server_now:
            try:
                server_clock = datetime.fromisoformat(self.last_server_now)
                if server_clock.tzinfo is None:
                    server_clock = server_clock.replace(tzinfo=timezone.utc)
                now_ref = max(now_ref, server_clock)
            except ValueError:
                pass
        return now_ref

    def _past_expiry(self) -> bool:
        """True if the license expiry has passed. Compares against the most
        conservative clock available: the later of the last trusted server time
        and the host wall clock. Returns True (fail closed) on a malformed
        expiry marker."""
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return self._now_anchored() > exp

    def reason_code(self) -> str:
        """Short stable code for the 503 body — clients switch on this."""
        if self.status == 'UNREGISTERED':
            return 'license_unregistered'
        if self.status == 'SUSPENDED':
            return 'license_suspended'
        if self.status == 'EXPIRED':
            return 'license_expired'
        if self.expires_at and self._past_expiry():
            return 'license_expired'
        if self.grace_until and self.is_blocked():
            return 'license_offline_grace_exceeded'
        return 'license_inactive'


def _to_iso(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def build_from_license(license_obj) -> LicenseState:
    """Snapshot a License row into a LicenseState dataclass."""
    grace_until = None
    if license_obj.last_heartbeat_at:
        grace_days = getattr(settings, 'LICENSE_GRACE_DAYS', 7)
        grace_until = license_obj.last_heartbeat_at + timedelta(days=grace_days)
    return LicenseState(
        status=license_obj.status,
        expires_at=_to_iso(license_obj.expires_at),
        last_heartbeat_at=_to_iso(license_obj.last_heartbeat_at),
        last_server_now=_to_iso(license_obj.last_server_now),
        grace_until=_to_iso(grace_until),
        message=license_obj.last_message or '',
        org_name=license_obj.org_name or '',
        email=license_obj.email or '',
        balance=str(license_obj.balance) if license_obj.balance is not None else None,
        days_remaining=license_obj.days_remaining,
        warn=bool(license_obj.warn),
    )


def get_state() -> LicenseState:
    """Hot path: middleware calls this on every non-allowlisted request."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return LicenseState(**cached)

    # Cache miss — derive from DB. Avoid importing License at module load
    # to keep Django's app registry happy.
    from licensing.models import License
    state = build_from_license(License.load())
    cache.set(CACHE_KEY, asdict(state), _cache_ttl())
    return state
