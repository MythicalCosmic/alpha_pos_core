"""Best-effort observers for exact local sync lifecycle evidence.

The core sync engine deliberately has no dependency on the desktop shell.  A
desktop build can register a tiny callback which copies events to its own
append-only evidence writer; server and test processes pay virtually no cost
when no observer is installed.  Observer failures must never affect checkout
or delivery of the real sync request.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_OBSERVERS: list[Callable[[str, dict[str, Any]], None]] = []


def register_sync_evidence_observer(
    callback: Callable[[str, dict[str, Any]], None],
) -> None:
    with _LOCK:
        if callback not in _OBSERVERS:
            _OBSERVERS.append(callback)


def unregister_sync_evidence_observer(
    callback: Callable[[str, dict[str, Any]], None],
) -> None:
    with _LOCK:
        if callback in _OBSERVERS:
            _OBSERVERS.remove(callback)


def emit_sync_evidence(event_type: str, **payload: Any) -> None:
    """Fan out an immutable-enough event without blocking the sync engine.

    Registered desktop callbacks must copy anything they retain.  This helper
    catches every observer exception because evidence is important, but must
    not become a second failure mode for the actual sale or sync path.
    """
    with _LOCK:
        observers = tuple(_OBSERVERS)
    for callback in observers:
        try:
            callback(str(event_type), payload)
        except Exception:  # noqa: BLE001 - sync remains authoritative
            logger.exception("sync evidence observer failed for %s", event_type)
