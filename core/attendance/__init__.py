"""AUTO_POS attendance hook.

``customers``/``waiters`` auth currently call ``hr.services.AttendanceService``
on every login/logout. To keep the local POS free of an ``hr`` import while still
writing the attendance row (the row must exist — HR ships to local as tables-only),
that write moves here behind ``ATTENDANCE_AUTO_POS``; it no-ops when ``hr`` isn't
installed (``apps.is_installed('hr')``).

TODO(Phase 1): implement ``pos_hook.auto_check_in/out`` and repoint the auth services.
"""
