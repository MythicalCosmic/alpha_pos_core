"""Operational states are separate from settlement, with explicit reopen rules."""
from base.helpers.response import ServiceResponse


TRANSITIONS = {
    'OPEN': {'PREPARING', 'CANCELED'},
    'PREPARING': {'READY', 'CANCELED'},
    'READY': {'COMPLETED', 'CANCELED'},
    'COMPLETED': {'CANCELED'},  # financial reversal is checked by the caller
    'CANCELED': set(),
}


def validate_transition(order, target, *, reopen=False):
    if target == order.status:
        return None
    if reopen and order.status == 'READY' and target == 'PREPARING' and not order.is_paid:
        return None
    if target not in TRANSITIONS.get(order.status, set()):
        return ServiceResponse.validation_error(
            {'status': f'Cannot change {order.status} to {target}.'},
            message='Illegal order status transition',
        )
    # Payment may precede kitchen completion. Advancing a paid PREPARING ticket
    # to READY remains valid; reopening paid work does not.
    if order.is_paid and target == 'PREPARING':
        return ServiceResponse.error('A paid ticket cannot be reopened')
    return None
