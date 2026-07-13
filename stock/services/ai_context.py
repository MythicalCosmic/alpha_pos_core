"""Shared location/branch scoping for AI read models.

The cloud database contains rows from every till.  AI endpoints receive a stock
``location_id`` from the client; that location is also the reliable way to learn
which branch's orders, shifts, and drawer the answer belongs to.  Keeping this in
one small helper prevents each analytics builder from inventing a different
fallback (most dangerously, ``CashRegister.objects.first()``).
"""
from dataclasses import dataclass

from django.conf import settings

from base.models import CashRegister
from stock.models import StockLocation


@dataclass(frozen=True)
class AIDataContext:
    """Resolved scope for one AI request."""

    location_id: int | None = None
    branch_id: str | None = None


def resolve_ai_context(location_id=None):
    """Return ``(context, error)`` for a client-supplied location.

    On a local till, ``BRANCH_ID`` is a safe fallback because that process owns a
    single branch.  On the cloud, absence of a location deliberately leaves the
    branch unset; callers may still return global catalog data, but must never
    choose an arbitrary branch register.
    """
    location = None
    if location_id not in (None, ''):
        try:
            location_pk = int(location_id)
        except (TypeError, ValueError):
            return None, f"invalid location_id: {location_id}"
        location = (StockLocation.objects.filter(
            id=location_pk, is_deleted=False,
        ).only('id', 'branch_id').first())
        if location is None:
            return None, f"stock location {location_pk} not found"

    branch_id = (location.branch_id or '').strip() if location else ''
    if not branch_id and getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'local':
        branch_id = str(getattr(settings, 'BRANCH_ID', '') or '').strip()
    if location is not None and not branch_id:
        return None, f"stock location {location.id} has no branch_id"

    return AIDataContext(
        location_id=location.id if location else None,
        branch_id=branch_id or None,
    ), None


def scope_branch(qs, branch_id, field='branch_id'):
    """Restrict a queryset when a branch context is available."""
    return qs.filter(**{field: branch_id}) if branch_id else qs


def scope_location_owned(qs, context, field='location'):
    """Scope rows whose authoritative owner is their ``location`` FK.

    Exact location wins.  For a branch-wide request, follow the location's
    branch rather than a legacy row's denormalized ``branch_id`` value, which
    may predate branch stamping or reflect the sync creator instead of owner.
    """
    if context.location_id:
        return qs.filter(**{f'{field}_id': context.location_id})
    if context.branch_id:
        return qs.filter(**{f'{field}__branch_id': context.branch_id})
    return qs


def scope_optional_location_owned(qs, context, field='location'):
    """Scope a nullable location owner while retaining shared/global rows.

    ``Recipe.production_location=None`` means the recipe is intentionally shared,
    unlike stock movements or purchase orders which always belong to one concrete
    location.  Such rows remain visible alongside the selected location/branch.
    """
    from django.db.models import Q

    visible = Q(**{f'{field}__isnull': True})
    if context.location_id:
        visible |= Q(**{f'{field}_id': context.location_id})
    elif context.branch_id:
        visible |= Q(**{f'{field}__branch_id': context.branch_id})
    else:
        return qs
    return qs.filter(visible)


def current_cash_register(branch_id=None):
    """Return the live register for the resolved branch, never an arbitrary one.

    A single-live-register fallback preserves useful behavior for older cloud
    clients that do not yet send ``location_id``.  Once two branches exist the
    result is intentionally ``None`` rather than silently reporting the first
    branch's money as everybody's balance.
    """
    qs = CashRegister.objects.filter(is_deleted=False)
    if branch_id:
        return qs.filter(branch_id=branch_id).first()
    candidates = list(qs.order_by('id')[:2])
    return candidates[0] if len(candidates) == 1 else None


__all__ = [
    'AIDataContext', 'resolve_ai_context', 'scope_branch', 'scope_location_owned',
    'scope_optional_location_owned', 'current_cash_register',
]
