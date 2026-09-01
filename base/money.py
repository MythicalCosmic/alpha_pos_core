"""Strict Decimal helpers for whole-UZS accounting commands."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.utils import timezone


UZS_QUANTUM = Decimal("1")
PERCENT_QUANTUM = Decimal("0.0001")
MAX_UZS = Decimal("99999999999999")


class MoneyValueError(ValueError):
    pass


def _plain_decimal(value, field):
    if isinstance(value, bool) or value is None:
        raise MoneyValueError(f"{field} must be a base-10 number")
    text = str(value).strip()
    if not text or "e" in text.lower():
        raise MoneyValueError(f"{field} must be a base-10 number")
    try:
        amount = Decimal(text)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MoneyValueError(f"{field} must be a base-10 number") from exc
    if not amount.is_finite():
        raise MoneyValueError(f"{field} must be finite")
    return amount


def whole_uzs(value, field="amount_uzs", *, positive=False, allow_zero=True,
              maximum=MAX_UZS):
    amount = _plain_decimal(value, field)
    if amount != amount.to_integral_value():
        raise MoneyValueError(f"{field} must be a whole UZS amount")
    if amount < 0 or (positive and amount <= 0) or (not allow_zero and amount == 0):
        qualifier = "greater than zero" if positive or not allow_zero else "non-negative"
        raise MoneyValueError(f"{field} must be {qualifier}")
    if amount > Decimal(maximum):
        raise MoneyValueError(f"{field} is too large")
    return amount.quantize(UZS_QUANTUM)


def signed_whole_uzs(value, field="amount_uzs", *, maximum=MAX_UZS):
    amount = _plain_decimal(value, field)
    if amount != amount.to_integral_value():
        raise MoneyValueError(f"{field} must be a whole UZS amount")
    if abs(amount) > Decimal(maximum):
        raise MoneyValueError(f"{field} is too large")
    return amount.quantize(UZS_QUANTUM)


def percentage(value, field="fee_percent"):
    amount = _plain_decimal(value, field)
    if amount < 0 or amount > 100:
        raise MoneyValueError(f"{field} must be between 0 and 100")
    if amount.as_tuple().exponent < -4:
        raise MoneyValueError(f"{field} must have at most four decimal places")
    return amount


def decimal_value(value, field, *, places=4, positive=False, allow_zero=True,
                  maximum=MAX_UZS):
    amount = _plain_decimal(value, field)
    if amount.as_tuple().exponent < -places:
        raise MoneyValueError(
            f'{field} must have at most {places} decimal places'
        )
    if amount < 0 or (positive and amount <= 0) or (not allow_zero and amount == 0):
        qualifier = 'greater than zero' if positive or not allow_zero else 'non-negative'
        raise MoneyValueError(f'{field} must be {qualifier}')
    if amount > Decimal(maximum):
        raise MoneyValueError(f'{field} is too large')
    return amount


def percentage_fee(amount_uzs, percent):
    return (amount_uzs * percent / Decimal("100")).quantize(
        UZS_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def uzs_int(value):
    return int(Decimal(value).quantize(UZS_QUANTUM, rounding=ROUND_HALF_UP))


def decimal_string(value, places=4):
    quantum = Decimal(1).scaleb(-places)
    return format(Decimal(value or 0).quantize(quantum), "f")


def local_iso(value):
    if value is None:
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.isoformat()
