"""License state for one POS install.

Singleton: there is exactly one License row per database (pk=1, enforced
in save()). The same pattern AppSettings / LoyaltySettings / NotificationSettings
use elsewhere in the project.

This model is deliberately host-local — it is NOT a SyncMixin, NOT
replicated across branches. Each restaurant's install has its own
license tied to its own DB.
"""
from django.conf import settings
from django.core.cache import cache
from django.db import models


class License(models.Model):
    class Status(models.TextChoices):
        # No setup wizard completed yet — middleware blocks everything except
        # the allowlisted setup / status endpoints.
        UNREGISTERED = 'UNREGISTERED', 'Unregistered'
        # Control center says "you are paid up". Heartbeats are fresh.
        ACTIVE = 'ACTIVE', 'Active'
        # Control center explicitly told us to stop. Middleware returns 503.
        SUSPENDED = 'SUSPENDED', 'Suspended'
        # `expires_at` is in the past (per control center's server_now).
        EXPIRED = 'EXPIRED', 'Expired'

    # Bearer token issued by the control center, stored encrypted at rest
    # (see services/state.py for the Fernet wrapper). Blob, not CharField,
    # because the encrypted form is bytes.
    key_encrypted = models.BinaryField(null=True, blank=True)

    org_name = models.CharField(max_length=200, default='')
    email = models.EmailField(default='')

    # Human-readable subscription plan name as reported by the control center
    # on the last successful heartbeat / register (e.g. "Standard"). Display-only
    # — enforcement keys off `status`/`expires_at`, never off this string. Older
    # control centers omit it; it then stays blank.
    plan_name = models.CharField(max_length=100, blank=True, default='')

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNREGISTERED,
    )

    # Subscription end as reported by the control center on the last
    # successful heartbeat. Compared against server_now (not local clock)
    # so an operator setting the host clock backwards can't extend it.
    expires_at = models.DateTimeField(null=True, blank=True)

    # Wall-clock of the last heartbeat that returned 2xx, in DB time. Used
    # to compute the offline-grace window.
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)

    # The server's view of `now` at the last successful heartbeat. Drives
    # expiry comparisons so we don't trust the local host clock.
    last_server_now = models.DateTimeField(null=True, blank=True)

    # Banner text the control center wants displayed in the POS UI. Set on
    # a per-tenant basis from the dashboard. NULL means "no banner".
    last_message = models.TextField(blank=True, default='')

    # Prepaid-billing snapshot from the last heartbeat. Display-only — the
    # control center is the source of truth and the kill switch keys off
    # `status` (EXPIRED), never off these numbers. `warn` is the control
    # center saying "inside the low-balance window — tell the operator to top
    # up", driven by the vendor-configured warn_days lead time.
    balance = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
    )
    days_remaining = models.IntegerField(null=True, blank=True)
    warn = models.BooleanField(default=False)

    # sha256(hostname + machine-id) — sent on each heartbeat so the control
    # center can flag installs that have been cloned (same key, multiple
    # fingerprints). Surface only; no auto-block.
    fingerprint = models.CharField(max_length=128, blank=True, default='')

    registered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'license'
        verbose_name_plural = 'license'
        constraints = [
            # Singleton at the DB layer. save() forces pk=1 too, but
            # bulk_create / raw SQL / objects.create(id=2) would otherwise
            # bypass it. Belt-and-braces.
            models.CheckConstraint(
                condition=models.Q(id=1),
                name='license_singleton_pk1',
            ),
        ]

    _CACHE_KEY = 'license:row:v1'

    def save(self, *args, **kwargs):
        # Singleton: always pk=1. Mirrors AppSettings.load() conventions in
        # base/models.py and the three settings models in notifications/.
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(self._CACHE_KEY)
        # Also bust the LicenseState cache the middleware reads on every
        # request; heartbeat_daemon writes both.
        cache.delete('license:state:v1')

    @classmethod
    def load(cls):
        cached = cache.get(cls._CACHE_KEY)
        if cached is not None:
            return cached
        obj, _ = cls.objects.get_or_create(pk=1)
        # Same TTL as the LicenseState snapshot so a cache miss reloads both
        # tiers at roughly the same cadence.
        ttl = getattr(settings, 'LICENSE_STATE_CACHE_TTL', 60)
        cache.set(cls._CACHE_KEY, obj, ttl)
        return obj

    def __str__(self):
        if self.status == self.Status.UNREGISTERED:
            return 'License (unregistered)'
        return f'License<{self.org_name or self.email or "?"} {self.status}>'


class LicenseEvent(models.Model):
    """Append-only local audit log for license actions.

    Deliberately NOT base.AuditLog — that table is SyncMixin'd and
    propagates to other branches. Licensing events are host-local: they
    describe what happened on THIS install, and should not leak to the
    central cloud or to sibling branches.
    """

    class Action(models.TextChoices):
        SETUP_ATTEMPTED = 'SETUP_ATTEMPTED', 'Setup attempted'
        SETUP_SUCCEEDED = 'SETUP_SUCCEEDED', 'Setup succeeded'
        HEARTBEAT_OK = 'HEARTBEAT_OK', 'Heartbeat OK'
        HEARTBEAT_FAILED = 'HEARTBEAT_FAILED', 'Heartbeat failed'
        STATUS_CHANGED = 'STATUS_CHANGED', 'Status changed'
        BLOCKED = 'BLOCKED', 'Request blocked by middleware'

    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'LicenseEvent<{self.action} @ {self.created_at:%Y-%m-%d %H:%M}>'
