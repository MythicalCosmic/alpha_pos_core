"""Conflict-safe custom-session extraction for ASGI WebSocket handshakes.

HTTP requests use ``resolve_session_credential``. WebSocket clients instead
send the raw session in ``?token=`` or an Authorization header, while browsers
also attach their existing ``session_key`` cookie automatically. All presented
identities must agree. A cookie by itself deliberately remains insufficient so
this helper does not expand the cross-site WebSocket authentication surface.
"""
from __future__ import annotations

import hmac
from http.cookies import CookieError, SimpleCookie
from typing import Any, Iterable
from urllib.parse import parse_qs

from base.helpers.request import SessionCredentialConflict


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(
        left.encode('utf-8', errors='surrogatepass'),
        right.encode('utf-8', errors='surrogatepass'),
    )


def _one_credential(
    candidates: Iterable[tuple[str, str]],
) -> tuple[str | None, list[str]]:
    selected: str | None = None
    sources: list[str] = []
    for value, source in candidates:
        value = str(value or '').strip()
        if not value:
            continue
        if selected is not None and not _same(selected, value):
            raise SessionCredentialConflict()
        selected = value
        if source not in sources:
            sources.append(source)
    return selected, sources


def _query_candidates(scope: dict[str, Any]) -> list[tuple[str, str]]:
    raw = scope.get('query_string') or b''
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='ignore')
    elif not isinstance(raw, str):
        raw = ''
    try:
        values = parse_qs(raw).get('token') or []
    except (TypeError, ValueError):
        values = []
    return [(value, 'query') for value in values]


def _header_candidates(
    scope: dict[str, Any], schemes: frozenset[str],
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for raw_key, raw_value in scope.get('headers') or []:
        try:
            key = bytes(raw_key).decode('ascii', errors='ignore').casefold()
            value = bytes(raw_value).decode('utf-8', errors='ignore')
        except (TypeError, ValueError):
            continue
        if key != 'authorization':
            continue
        scheme, separator, credential = value.partition(' ')
        source = scheme.casefold()
        if separator and source in schemes and credential.strip():
            candidates.append((credential.strip(), source))
    return candidates


def _cookie_candidates(scope: dict[str, Any]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for raw_key, raw_value in scope.get('headers') or []:
        try:
            key = bytes(raw_key).decode('ascii', errors='ignore').casefold()
            value = bytes(raw_value).decode('latin-1', errors='ignore')
        except (TypeError, ValueError):
            continue
        if key != 'cookie':
            continue
        parsed = SimpleCookie()
        try:
            parsed.load(value)
        except CookieError:
            continue
        morsel = parsed.get('session_key')
        if morsel is not None and morsel.value:
            candidates.append((morsel.value, 'cookie'))
    return candidates


def resolve_websocket_session_credential(
    scope: dict[str, Any], *, header_schemes: Iterable[str] = ('bearer',),
) -> tuple[str | None, str | None]:
    """Return a conflict-safe WebSocket session and its transport sources.

    The primary transport remains ``?token=`` or an allowed Authorization
    scheme. Duplicate/mixed transports are accepted only when every value is
    identical. A presented browser cookie must match that primary credential,
    but a cookie alone does not authenticate the socket.
    """
    schemes = frozenset(
        str(scheme).strip().casefold()
        for scheme in header_schemes
        if str(scheme).strip()
    )
    selected, sources = _one_credential([
        *_query_candidates(scope),
        *_header_candidates(scope, schemes),
    ])
    cookie, cookie_sources = _one_credential(_cookie_candidates(scope))
    if selected and cookie and not _same(selected, cookie):
        raise SessionCredentialConflict()
    if not selected:
        return None, None
    if cookie:
        sources.extend(
            source for source in cookie_sources if source not in sources
        )
    return selected, '+'.join(sources)
