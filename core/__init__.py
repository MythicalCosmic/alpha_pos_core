"""Shared shims introduced by the edition split.

These are pieces that had to be lifted OUT of a one-sided app so both editions can
use them without importing each other:

* ``core.shifts``     — ShiftService relocated out of ``admins``.
* ``core.attendance`` — AUTO_POS attendance write, so the local POS stops importing
                        ``hr.services`` (HR ships to local as tables-only).
* ``core.realtime``   — Channels consumers + ``publish.py`` (group_send helpers).
* ``core.sync_ws``    — websocket transport/consumer that REUSE base/services/sync/*.

See ../../WORKSPACE.md for the migration plan. Implementations land in Phase 1+.
"""
