"""Per-shift drawer math (derived, never stored).

A shift's expected money is computed from OrderPayment rows in the shift's paid
window, grouped by tender type; the CASH figure is net of cashbox expenses paid
out of the drawer. Returns/cancels are already netted because cancelling a paid
order reverses its OrderPayment-driven cash.
"""
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from base.models import OrderPayment
from cashbox.models import CashboxExpense, PAYMENT_METHODS


def expected_payment_totals(shift):
    """{method: expected_amount} for the shift. CASH is net of cashbox expenses."""
    end = shift.end_time or timezone.now()
    rows = (
        OrderPayment.objects.filter(
            is_deleted=False,
            order__is_deleted=False,
            order__cashier_id=shift.user_id,
            order__paid_at__gte=shift.start_time,
            order__paid_at__lte=end,
        )
        .exclude(order__status='CANCELED')
        .values('method')
        .annotate(total=Sum('amount'))
    )
    totals = {m: Decimal('0.00') for m in PAYMENT_METHODS}
    for r in rows:
        method = (r['method'] or 'CASH').upper()
        totals[method] = totals.get(method, Decimal('0.00')) + (r['total'] or Decimal('0'))

    cash_expenses = (
        CashboxExpense.objects.filter(shift=shift, is_deleted=False)
        .aggregate(s=Sum('amount'))['s'] or Decimal('0')
    )
    totals['CASH'] = totals.get('CASH', Decimal('0.00')) - cash_expenses
    return totals


def drawer_cash(shift):
    """Live physical cash that should be in the drawer right now."""
    return expected_payment_totals(shift).get('CASH', Decimal('0.00'))
