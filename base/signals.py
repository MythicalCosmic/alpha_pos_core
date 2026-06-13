"""Signal wiring for the base app.

Kept narrow on purpose: signals are easy to over-use and hard to track in
review. Only register things here that are genuinely cross-cutting (caches
that shadow ORM state, etc.).
"""
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from base.models import Session, User


@receiver(post_save, sender=User)
def _invalidate_user_session_cache(sender, instance, **kwargs):
    """Flush a user's cached sessions whenever the user row changes.

    Sessions are cached with their joined user for SESSION_CACHE_TTL, so a
    suspend / role-change / deactivate would keep granting the old access for
    up to the TTL. We invalidate on every user save (rare events) rather than
    diffing fields — correctness over a negligible cache miss. The session
    rows are untouched; only the cache entries are dropped.
    """
    from base.repositories.session import SessionRepository
    SessionRepository.invalidate_user_cache(instance)


@receiver(post_delete, sender=Session)
def _invalidate_session_cache(sender, instance, **kwargs):
    """Drop the cached Session row when its DB row is deleted.

    `SessionRepository.get_by_session_key` caches sessions for
    SESSION_CACHE_TTL (5 min by default), and the explicit `logout` /
    `delete_by_user` paths call `invalidate_cache` directly. But any other
    deletion route — django admin, a management command, a raw ORM delete —
    would leave the cached row valid for up to TTL seconds, so a revoked
    token kept working. The signal closes that window deterministically.
    """
    payload = getattr(instance, 'payload', None)
    if payload:
        cache.delete(f"session:{payload}")
