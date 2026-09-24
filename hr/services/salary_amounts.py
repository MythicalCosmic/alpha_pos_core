"""The DecimalField boundary shared by salary commands and child items."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MAX_SALARY_AMOUNT = Decimal('9999999999.99')


def salary_amount(value, *, allow_negative=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) > MAX_SALARY_AMOUNT:
            raise ValueError
        if amount < 0 and not allow_negative:
            raise ValueError
        return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError('Use a finite amount within the salary field limit; components must be non-negative.') from exc
