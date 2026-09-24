"""Protect cloud expense commands while retaining legacy drawer evidence.

Legacy expenses without canonical workflow history remain ingestible. This is
not a migration of those records into the funded treasury workflow.
"""
from django.conf import settings


def branch_write_forbidden(model, data, *, existing=None):
    if getattr(settings, 'DEPLOYMENT_MODE', 'local') != 'cloud':
        return False
    label = model._meta.label_lower
    if label == 'hr.expensetransition':
        # Approval/payment audit history is authored by the cloud command,
        # never by an assertion supplied with a branch token.
        return True
    if label != 'hr.expense':
        return False
    if str(data.get('requested_source') or '').upper() in {'SAFE', 'BANK'}:
        return True
    if any(data.get(field) for field in (
        'treasury_transaction', 'treasury_transaction_id',
        'treasury_reversal', 'treasury_reversal_id',
    )):
        return True
    if existing is None:
        return False
    return bool(
        existing.requested_source in {'SAFE', 'BANK'}
        or existing.treasury_transaction_id
        or existing.treasury_reversal_id
        or existing.payment_action_id
        or existing.void_action_id
        # Drawer rows the till recorded carry the cloud's mirror of their
        # history too (every one on production does), and the till still owns
        # later edits to them. History marks a cloud workflow row otherwise.
        or (existing.requested_source != 'DRAWER' and existing.transitions.exists())
    )
