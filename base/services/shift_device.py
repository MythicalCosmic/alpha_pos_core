"""Device ownership rules for cashier money mutations.

The Shift row is synchronized data, but ``settings.DEVICE_ID`` is the stable
identity of the installation executing the mutation.  A legacy blank-device
shift may still be closed after an upgrade; it must never authorize another
checkout, refund, or drawer payout.  Manager/admin shifts deliberately remain
outside the physical cashier-slot rule.
"""

from django.conf import settings


TERMINAL_DEVICE_ID_MISSING = (
    'This terminal has no valid device identity. Restart the desktop app '
    'before starting or settling a cashier shift.'
)
TERMINAL_DEVICE_ID_INVALID = (
    'This terminal has an invalid device identity. Repair the installation '
    'before starting or settling a cashier shift.'
)
CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL = (
    'Cashier active shift is not bound to this terminal. Close it and start '
    'a new shift on this terminal before taking payment.'
)

MAX_DEVICE_ID_LENGTH = 128


def terminal_device_id():
    """Return this installation's normalized identity, or ``''`` on cloud."""
    if getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud':
        return ''
    return str(getattr(settings, 'DEVICE_ID', '') or '').strip()


def terminal_device_identity_error():
    """Stable configuration error for a local cashier installation."""
    device_id = terminal_device_id()
    if not device_id:
        return TERMINAL_DEVICE_ID_MISSING
    if len(device_id) > MAX_DEVICE_ID_LENGTH:
        return TERMINAL_DEVICE_ID_INVALID
    return None


def cashier_shift_device_error(user, shift):
    """Return why ``shift`` cannot settle cashier money on this process.

    Cloud administration and non-cashier staff intentionally keep their
    existing semantics. A local CASHIER must have their ACTIVE shift owned by
    this exact desktop installation. Authenticated login may claim a blank
    legacy shift before this guard is used; another installation's identity is
    always denied.
    """
    role = str(getattr(user, 'role', '') or '').upper()
    if (
        role != 'CASHIER'
        or getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud'
    ):
        return None

    identity_error = terminal_device_identity_error()
    if identity_error:
        return identity_error

    if str(getattr(shift, 'device_id', '') or '').strip() != terminal_device_id():
        return CASHIER_SHIFT_NOT_BOUND_TO_TERMINAL
    return None
