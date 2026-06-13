"""Fernet wrapper for encrypting the license key at rest.

The license key is the only secret the License row carries. Storing it
in cleartext would mean a DB dump (backups, support exports, accidental
log line) leaks credentials that let the holder act as this tenant
against the control center. Encrypt with Fernet keyed by
LICENSE_FERNET_KEY so a stolen DB still requires the env var.

Operator UX: run.py auto-generates LICENSE_FERNET_KEY into .license_fernet_key
on first boot, so the non-tech "double-click start.bat" install path never
sees a manual key step. Production deployments that don't use run.py must set
LICENSE_FERNET_KEY explicitly (we refuse to derive a fallback there — silently
relying on SECRET_KEY would break license decryption the moment SECRET_KEY
ever rotates).

Dev fallback (DEBUG=True only): if LICENSE_FERNET_KEY is unset, derive a key
deterministically from SECRET_KEY so the test suite and ad-hoc `runserver`
work without extra setup.
"""
import base64
import hashlib
import logging
import os
from typing import Optional

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from cryptography.fernet import Fernet, InvalidToken


logger = logging.getLogger(__name__)


def _resolve_fernet_key() -> bytes:
    """Return a 32-byte urlsafe-base64 Fernet key.

    Priority: settings.LICENSE_FERNET_KEY → os.environ['LICENSE_FERNET_KEY']
    → SECRET_KEY-derived (DEBUG only). In non-DEBUG with no LICENSE_FERNET_KEY
    we raise instead of silently falling back — SECRET_KEY rotation must not
    be allowed to invalidate stored license keys without anyone noticing.

    Env fallback covers the test-harness case where pytest-django loads
    settings.py before conftest gets to set the env var. In production both
    paths agree (run.py exports it before django.setup() runs).
    """
    explicit = getattr(settings, 'LICENSE_FERNET_KEY', '') or os.environ.get(
        'LICENSE_FERNET_KEY', '',
    )
    if explicit:
        return explicit.encode('utf-8') if isinstance(explicit, str) else explicit

    if not getattr(settings, 'DEBUG', False):
        raise ImproperlyConfigured(
            'LICENSE_FERNET_KEY is not configured. Generate one with '
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and set it in the '
            'environment, or boot via run.py which generates and persists '
            'it automatically.'
        )

    digest = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_resolve_fernet_key())


def encrypt_key(cleartext: str) -> bytes:
    """Encrypt a license key for at-rest storage. Returns bytes safe for
    a BinaryField."""
    if not cleartext:
        return b''
    return _fernet().encrypt(cleartext.encode('utf-8'))


def decrypt_key(blob: bytes) -> Optional[str]:
    """Decrypt a previously-stored license key. Returns None on tamper /
    wrong-key (e.g. the operator rotated LICENSE_FERNET_KEY)."""
    if not blob:
        return None
    try:
        return _fernet().decrypt(bytes(blob)).decode('utf-8')
    except InvalidToken:
        logger.error(
            'License key decryption failed — LICENSE_FERNET_KEY may have '
            'rotated. The operator must re-run the setup wizard to issue '
            'a new key.'
        )
        return None
