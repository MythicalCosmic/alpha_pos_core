import hashlib

from django.core.cache import cache
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
        # Authorization must reflect the current committed session and user.
        # Eviction alone cannot prevent a concurrent reader from caching an
        # old row after revocation commits. One indexed, joined lookup also
        # works across processes and does not publish uncommitted identities.
        return cls.model.objects.select_related('user_id').filter(payload=token_hash).first()

    @classmethod
    def invalidate_cache(cls, session_key):
        token_hash = cls.hash_token(session_key)
        if token_hash:
            cache.delete(f"session:{token_hash}")

    @classmethod
    def invalidate_user_cache(cls, user):
        """Evict legacy session entries without deleting valid sessions.

        Authentication reads the database directly. Keep this compatibility
        cleanup for callers and entries written by earlier application builds.
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
