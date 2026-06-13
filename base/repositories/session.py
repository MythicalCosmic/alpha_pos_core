import hashlib

from django.core.cache import cache
from django.conf import settings
from base.repositories.base import BaseRepository
from base.models import Session


class SessionRepository(BaseRepository):
    model = Session

    @staticmethod
    def hash_token(token):
        """Map a raw bearer token to the value stored in `Session.payload`.

        Tokens are persisted (DB + Redis) only as their SHA-256 digest so a
        Redis dump / `MONITOR` / DB read cannot yield live, directly-usable
        session tokens. The raw token is held only by the client. The cache
        key is derived from the digest too, so the token never appears in the
        Redis key space either. SHA-256 (not a slow KDF) is correct here: the
        input is 256 bits of CSPRNG output (secrets.token_hex(32)), so there
        is nothing to brute-force.
        """
        if not token:
            return None
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @classmethod
    def get_by_session_key(cls, session_key):
        token_hash = cls.hash_token(session_key)
        if not token_hash:
            return None
        cache_key = f"session:{token_hash}"
        ttl = getattr(settings, 'SESSION_CACHE_TTL', 300)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        session = cls.model.objects.select_related('user_id').filter(payload=token_hash).first()
        if session:
            cache.set(cache_key, session, ttl)
        return session

    @classmethod
    def invalidate_cache(cls, session_key):
        token_hash = cls.hash_token(session_key)
        if token_hash:
            cache.delete(f"session:{token_hash}")

    @classmethod
    def invalidate_user_cache(cls, user):
        """Drop the cached Session rows for a user *without* deleting them.

        get_by_session_key caches the session together with its joined user
        for SESSION_CACHE_TTL (5 min). When an admin suspends a user or
        changes their role, that stale joined user keeps granting the old
        access until the entry expires. Clearing the cache forces the next
        request to re-read the user fresh; the sessions themselves stay valid.
        """
        payloads = cls.model.objects.filter(user_id=user).values_list('payload', flat=True)
        for payload in payloads:
            if payload:
                cache.delete(f"session:{payload}")

    @classmethod
    def get_by_user(cls, user):
        return cls.model.objects.filter(user_id=user)

    @classmethod
    def get_latest_by_user(cls, user):
        return cls.model.objects.filter(user_id=user).order_by('-last_activity').first()

    @classmethod
    def delete_by_user(cls, user):
        sessions = cls.model.objects.filter(user_id=user)
        for s in sessions:
            # s.payload is already the stored hash — the cache key mirrors it.
            cache.delete(f"session:{s.payload}")
        sessions.delete()

    @classmethod
    def delete_by_user_except(cls, user, except_session_key):
        # Used by change-password to revoke every session except the one
        # making the change, so a leaked token doesn't survive remediation.
        except_hash = cls.hash_token(except_session_key)
        sessions = cls.model.objects.filter(user_id=user).exclude(payload=except_hash)
        for s in sessions:
            cache.delete(f"session:{s.payload}")
        sessions.delete()
