"""Per-shift drawer math (derived, never stored).

A shift's expected money is the tender split of the orders paid in the shift's
window by that cashier; the CASH figure is net of cashbox expenses paid out of the
drawer. Returns/cancels are already netted because a cancelled order is excluded.

CASH IS DERIVED, NOT SUMMED. ``OrderPayment`` stores the cash TENDERED — which
includes the change handed back out of the same drawer — while the till only
credits ``effective_total - noncash`` (customers.order_service ``mark_as_paid``).
Summing the raw CASH lines therefore over-states the drawer by exactly the change
and flags the cashier SHORT by that amount. Attribution is delegated to
``base.services.tender`` so the drawer, the dashboard and the shift roll-up all
use one implementation (and so an order with no payment lines — courier / admin —
is bucketed by its rolled-up method instead of silently becoming cash).
"""
import logging
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from base.models import Order
from base.services.tender import breakdown_for_orders
from cashbox.models import CashboxExpense, PAYMENT_METHODS

logger = logging.getLogger(__name__)


def _shift_orders(shift):
    """Orders whose money landed in this shift: paid inside the window by this
    cashier, excluding cancellations."""
    end = shift.end_time or timezone.now()
    return Order.objects.filter(
        is_deleted=False,
        cashier_id=shift.user_id,
        paid_at__gte=shift.start_time,
        paid_at__lte=end,
    ).exclude(status='CANCELED')


def expected_payment_totals(shift):
    """{method: expected_amount} for the shift. CASH is net of cashbox expenses.

    Non-cash tenders settle externally and are reported per acquirer (UZCARD /
    HUMO / CARD) so a bank statement can still be reconciled line by line.
    """
    orders = _shift_orders(shift)
    split, card_detail = breakdown_for_orders(orders)

    totals = {m: Decimal('0.00') for m in PAYMENT_METHODS}
    for method, amount in card_detail.items():
        if method in totals:
            totals[method] = amount
    totals['PAYME'] = split['payme']

    cash_expenses = (
        CashboxExpense.objects.filter(shift=shift, is_deleted=False)
        .aggregate(s=Sum('amount'))['s'] or Decimal('0')
    )
    totals['CASH'] = split['cash'] - cash_expenses

    if split['unknown']:
        # Money we cannot attribute to a tender (e.g. a MIXED order with no payment
        # lines). Never guess — the expected totals are knowingly short by this much
        # and base.services.tender.unattributed_orders() is the canary.
        logger.error('drawer: shift %s has %s unattributable revenue (no payment lines)',
                     shift.id, split['unknown'])
    return totals


def drawer_cash(shift):
    """Live physical cash that should be in the drawer right now."""
    return expected_payment_totals(shift).get('CASH', Decimal('0.00'))
