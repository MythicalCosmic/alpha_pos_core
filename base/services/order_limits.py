"""Validate projected order writes against the shared storage contract."""
from decimal import Decimal

from base.helpers.request import MAX_QUANTITY
from base.helpers.response import ServiceResponse
from base.models import Order


def validate_quantity(quantity):
    if (not isinstance(quantity, int) or isinstance(quantity, bool)
            or not 0 < quantity <= MAX_QUANTITY):
        return ServiceResponse.validation_error(
            errors={'quantity': f'Must be an integer from 1 to {MAX_QUANTITY}'},
            message='Invalid quantity',
        )
    return None


def validate_order_subtotal(subtotal):
    # Use the model capacity so SQLite accepts the same orders PostgreSQL can
    # store. Discounting an oversized subtotal cannot make that column fit.
    field = Order._meta.get_field('subtotal')
    maximum = Decimal(10) ** (field.max_digits - field.decimal_places) - Decimal(10) ** -field.decimal_places
    if not subtotal.is_finite() or not Decimal('0') <= subtotal <= maximum:
        return ServiceResponse.validation_error(
            errors={'items': f'Order subtotal must be between 0 and {maximum}'},
            message='Order subtotal exceeds supported storage limits',
        )
    return None


def validate_item_change(order, *, quantity, price, replacing=None):
    """Call under the parent order lock, before writing the projected line."""
    error = validate_quantity(quantity)
    if error:
        return error
    items = order.items.filter(is_deleted=False)
    if replacing is not None:
        items = items.exclude(pk=replacing.pk)
    subtotal = sum(
        (line_price * line_quantity for line_price, line_quantity
         in items.values_list('price', 'quantity')),
        Decimal('0'),
    ) + price * quantity
    return validate_order_subtotal(subtotal)
