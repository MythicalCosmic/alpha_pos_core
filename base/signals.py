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
    """Evict joined identities left in the cache by older builds.

    Current authentication queries the session and user directly. Retain this
    cleanup for compatibility; access revocation no longer depends on eviction.
    """
    from base.repositories.session import SessionRepository
    SessionRepository.invalidate_user_cache(instance)


@receiver(post_delete, sender=Session)
def _invalidate_session_cache(sender, instance, **kwargs):
    """Remove a deleted session's legacy cache entry on every ORM route.

    Current authentication never trusts these entries, including entries a
    concurrent legacy reader might refill after this signal runs.
    """
    payload = getattr(instance, 'payload', None)
    if payload:
        cache.delete(f"session:{payload}")
