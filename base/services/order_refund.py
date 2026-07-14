"""Paid-order settlement guards and append-only refund recording.

The Order row describes the sale.  Its paid fields and OrderPayment children
are immutable historical facts once settled.  Cancellation is operational;
when it returns money, this module records a second, dated accounting event.
"""
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone


class SettlementInvariantError(ValueError):
    """A payment/refund would violate shift or branch ownership."""


def lock_active_cashier_shift(cashier_id, *, branch_id=''):
    """Return and lock the cashier's one usable ACTIVE shift.

    Every register mutation must be owned by an on-duty cashier.  The branch
    comparison is deliberately fail-closed: a cloud/global process must not
    book one branch's order into another branch's drawer.
    """
    from base.models import Shift, User

    if not cashier_id:
        raise SettlementInvariantError(
            'An active cashier shift is required to settle money.'
        )

    # Lock both ownership records.  Dispatch and settlement may race shift
    # closure/user reassignment; the lock keeps the validation true until the
    # surrounding transaction commits.
    cashier = (
        User.objects.select_for_update()
        .filter(pk=cashier_id, is_deleted=False)
        .first()
    )
    if cashier is None:
        raise SettlementInvariantError('Cashier not found or inactive.')

    shifts = list(
        Shift.objects.select_for_update()
        .filter(
            user_id=cashier_id,
            status=Shift.Status.ACTIVE,
            is_deleted=False,
            end_time__isnull=True,
        )
        .order_by('-start_time', '-pk')[:2]
    )
    if not shifts:
        raise SettlementInvariantError(
            'Cashier must start an active shift before taking or refunding payment.'
        )
    if len(shifts) > 1:
        # Silently choosing one would make drawer attribution nondeterministic.
        raise SettlementInvariantError(
            'Cashier has multiple active shifts; close the duplicate shift first.'
        )

    shift = shifts[0]
    expected_branch = str(branch_id or '').strip()
    explicit_shift_branch = str(shift.branch_id or '').strip()
    cashier_branch = str(cashier.branch_id or '').strip()
    cashier_is_global = (
        getattr(type(cashier), 'SYNC_PULL_SCOPE', 'branch') == 'global'
    )
    if (explicit_shift_branch and cashier_branch and not cashier_is_global
            and explicit_shift_branch != cashier_branch):
        raise SettlementInvariantError(
            'Cashier and active shift belong to different branches.'
        )
    # The shift is the operational branch authority. Cloud-scoped User rows are
    # global identities in production and intentionally do not override it.
    shift_branch = explicit_shift_branch
    if not shift_branch:
        raise SettlementInvariantError(
            'Cashier active shift has no branch ownership.'
        )
    if expected_branch and expected_branch != shift_branch:
        raise SettlementInvariantError(
            'Cashier active shift belongs to a different branch.'
        )
    return shift


@transaction.atomic
def record_paid_order_refund(order_or_id, cashier_id, *, reason=''):
    """Create the one immutable ORDER_CANCEL refund for a paid order.

    Returns ``(refund, created)``.  A retry returns the existing event without
    moving its timestamp, touching the drawer again, or requiring the old shift
    to still be open.  Unpaid cancellation returns ``(None, False)`` because it
    has no settlement to reverse.
    """
    from base.models import Inkassa, Order, OrderRefund
    from base.services.accounting_cursor import lock_branch_accounting
    from base.services.tender import order_tender_sources

    order_id = getattr(order_or_id, 'pk', order_or_id)
    order = Order.objects.select_for_update().get(pk=order_id)

    existing = (
        OrderRefund.objects.select_for_update()
        .filter(
            order_id=order.id,
            source=OrderRefund.Source.ORDER_CANCEL,
            is_deleted=False,
        )
        .first()
    )
    if existing is not None:
        return existing, False

    if not order.is_paid:
        return None, False
    if order.paid_at is None:
        raise SettlementInvariantError(
            'Paid order has no settlement timestamp; repair it before refunding.'
        )

    shift = lock_active_cashier_shift(
        cashier_id, branch_id=order.branch_id,
    )
    refund_branch = str(order.branch_id or shift.branch_id or shift.user.branch_id).strip()
    split, card_detail, original_drawer_cash = order_tender_sources(order)
    split = {key: Decimal(value or 0) for key, value in split.items()}
    expected = Decimal(order.total_amount or 0)
    if sum(split.values(), Decimal('0.00')) != expected:
        raise SettlementInvariantError(
            'Original tender split does not equal the immutable order total.'
        )

    # Provider callbacks may already have refunded one or more partial courier
    # collections. Cancel returns only the still-unreversed remainder; otherwise
    # the same money would be subtracted twice in the ledger and analytics.
    prior_qs = OrderRefund.objects.select_for_update().filter(
        order=order, is_deleted=False,
    )
    prior = refund_totals(prior_qs)
    remaining = {
        'cash': split['cash'] - prior['cash_amount'],
        'card': split['card'] - prior['card_amount'],
        'payme': split['payme'] - prior['payme_amount'],
        'unknown': split['unknown'] - prior['unknown_amount'],
    }
    drawer_cash = Decimal(original_drawer_cash or 0) - prior['drawer_cash_amount']
    if any(value < 0 for value in remaining.values()) or drawer_cash < 0:
        raise SettlementInvariantError(
            'Existing refund events exceed the order tender evidence.'
        )
    amount = sum(remaining.values(), Decimal('0.00'))
    if amount != expected - prior['amount']:
        raise SettlementInvariantError(
            'Existing refund totals do not reconcile with the order total.'
        )
    if amount == 0:
        previous = prior_qs.order_by('-refunded_at', '-pk').first()
        return previous, False

    remaining_detail = {
        key: Decimal(value or 0) for key, value in card_detail.items()
    }
    for frozen in prior_qs.values_list('card_detail', flat=True):
        for key in remaining_detail:
            remaining_detail[key] -= Decimal((frozen or {}).get(key) or 0)
    if any(value < 0 for value in remaining_detail.values()):
        raise SettlementInvariantError(
            'Existing card refund detail exceeds the original tender evidence.'
        )

    remote_cash_command = (
        getattr(settings, 'DEPLOYMENT_MODE', 'local') == 'cloud'
        and bool(drawer_cash)
    )

    # Serialize every physical-cash refund with inkassa, expenses, payments,
    # and other refunds.  On cloud the synchronized balance is only the last
    # branch report, so subtract every issued-but-unacknowledged command before
    # deciding whether another remote cash-out is safe.
    # Every cancellation refund advances the branch-local accounting cursor.
    # Courier handover takes these same branch locks before its cutoff and reads
    # accounting_recorded_at, so a late/blocked refund rolls forward exactly once.
    register = lock_branch_accounting(refund_branch)
    register_balance = Decimal('0.00')
    if drawer_cash:
        register_balance = Decimal(register.current_balance or 0)
        pending = Inkassa.pending_register_amount(register)
        available = register_balance - pending
        if drawer_cash > available:
            raise SettlementInvariantError(
                'The cash register does not contain enough available cash '
                f'to refund {drawer_cash:.2f}; available {available:.2f}.'
            )

    stored_reason = (reason or '')[:255]
    refund = OrderRefund.objects.create(
        order=order,
        shift=shift,
        cashier=shift.user,
        amount=amount,
        cash_amount=remaining['cash'],
        drawer_cash_amount=drawer_cash,
        card_amount=remaining['card'],
        payme_amount=remaining['payme'],
        unknown_amount=remaining['unknown'],
        card_detail={key: str(value) for key, value in remaining_detail.items()},
        refunded_at=timezone.now(),
        # The immutable refund is the accounting event. A companion Inkassa
        # row below is the cash transport command because even pre-upgrade
        # desktops understand Inkassa and retain its notes marker.
        register_command=False,
        source=OrderRefund.Source.ORDER_CANCEL,
        source_id=str(order.uuid),
        reason=stored_reason,
        branch_id=refund_branch,
    )

    # Only physical cash entered the register.  Card/Payme refunds settle at
    # their acquirers, but remain visible as negative tender events in reports.
    if refund.drawer_cash_amount and remote_cash_command:
        Inkassa.objects.create(
            cashier=shift.user,
            amount=refund.drawer_cash_amount,
            inkass_type=Inkassa.InkassType.CASH,
            balance_before=available,
            balance_after=available - refund.drawer_cash_amount,
            total_orders=0,
            total_revenue=0,
            register_command=True,
            notes=Inkassa.refund_command_notes(refund, order, reason),
            branch_id=refund_branch,
        )
    elif refund.drawer_cash_amount:
        register.current_balance = register_balance - refund.drawer_cash_amount
        register.last_updated = timezone.now()
        register.save(update_fields=[
            'current_balance', 'last_updated', 'synced_at', 'sync_version',
        ])
    return refund, True


@transaction.atomic
def record_external_provider_refund(
        order_or_id, *, method, amount, source_id, reason='', refunded_at=None):
    """Record one immutable shiftless provider refund.

    Courier cash/card/QR never entered a POS drawer, so this path creates no
    CashRegister or Inkassa movement. The original provider payment and Order
    paid header remain positive sale evidence; reports subtract this event on
    ``refunded_at``.
    """
    from base.models import Order, OrderRefund
    from base.services.tender import bucket_for, empty_detail, empty_split, normalize_method

    event_id = str(source_id or '').strip()
    if not event_id:
        raise SettlementInvariantError('Provider refund requires an idempotency key.')

    order_id = getattr(order_or_id, 'pk', order_or_id)
    order = Order.objects.select_for_update().get(pk=order_id)
    existing = OrderRefund.objects.select_for_update().filter(
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id=event_id,
        is_deleted=False,
    ).first()
    if existing is not None:
        if existing.order_id != order.id:
            raise SettlementInvariantError(
                'Provider refund identity belongs to another order.'
            )
        return existing, False

    if not order.is_paid or order.paid_at is None:
        raise SettlementInvariantError(
            'Provider refund requires an immutable paid order settlement.'
        )
    branch_id = str(order.branch_id or '').strip()
    if not branch_id:
        raise SettlementInvariantError('Paid order has no branch ownership.')
    value = Decimal(str(amount or 0))
    if not value.is_finite() or value <= 0:
        raise SettlementInvariantError('Provider refund amount must be positive.')

    prior = refund_totals(OrderRefund.objects.select_for_update().filter(
        order=order, is_deleted=False,
    ))
    remaining = Decimal(order.total_amount or 0) - prior['amount']
    if value > remaining:
        raise SettlementInvariantError(
            f'Provider refund exceeds unreversed order amount {remaining:.2f}.'
        )

    normalized = normalize_method(method)
    bucket = bucket_for(normalized)
    if bucket is None:
        raise SettlementInvariantError('Provider refund method is not supported.')
    split = empty_split()
    split[bucket] = value
    detail = empty_detail()
    if bucket == 'card' and normalized in detail:
        detail[normalized] = value

    refund = OrderRefund.objects.create(
        order=order,
        shift=None,
        cashier=None,
        amount=value,
        cash_amount=split['cash'],
        drawer_cash_amount=Decimal('0.00'),
        card_amount=split['card'],
        payme_amount=split['payme'],
        unknown_amount=split['unknown'],
        card_detail={key: str(amount) for key, amount in detail.items()},
        refunded_at=refunded_at or timezone.now(),
        register_command=False,
        source=OrderRefund.Source.COURIER_PAYMENT,
        source_id=event_id,
        reason=(reason or '')[:255],
        branch_id=branch_id,
    )
    return refund, True


def refund_totals(refund_qs):
    """Aggregate frozen tender buckets for a refund queryset."""
    from django.db.models import DecimalField, Sum
    from django.db.models.functions import Coalesce

    zero = Decimal('0.00')
    output = DecimalField(max_digits=18, decimal_places=2)
    return refund_qs.aggregate(**{
        field: Coalesce(Sum(field), zero, output_field=output)
        for field in (
            'amount', 'cash_amount', 'drawer_cash_amount', 'card_amount',
            'payme_amount', 'unknown_amount',
        )
    })
