"""Cart service for the Telegram bot.

The cart is a small server-side workspace tied to a TelegramCustomer.
Customers add / remove items across messages, then /order checkout
converts it to a real base.Order.

Conventions:
  - Exactly one ACTIVE cart per customer at any time (DB constraint).
  - Item rows are upserted: re-adding a product bumps quantity instead of
    making a duplicate row.
  - Price is snapshotted at add time. If a manager changes price during
    the customer's browsing, the customer sees what they were quoted —
    not what the manager just typed.

Order creation provisions a base.User if the TelegramCustomer doesn't
have one linked yet. The provisioned user has role USER, a generated
email keyed on chat_id, and an unusable password — it exists only to
satisfy the Order.user FK. Future /order checkouts reuse the same user.
"""
import logging
import secrets
from decimal import Decimal

from django.db import IntegrityError, transaction

from base.models import Order, OrderItem, Product, User
from notifications.models import Cart, CartItem
# Reuse the QR self-order caps so both self-serve surfaces are bounded the same
# way: at most MAX_QUANTITY_PER_LINE of one product, at most MAX_ITEMS_PER_ORDER
# distinct lines per cart.
from notifications.services.qr_order_service import (
    MAX_ITEMS_PER_ORDER, MAX_QUANTITY_PER_LINE,
)

logger = logging.getLogger(__name__)


def get_or_create_active_cart(customer):
    try:
        cart, _ = Cart.objects.get_or_create(
            customer=customer, status=Cart.Status.ACTIVE,
        )
        return cart
    except IntegrityError:
        # Concurrent first-add: two near-simultaneous taps both miss the
        # existing row and try to INSERT, but the one_active_cart_per_customer
        # partial unique constraint rejects the loser. Re-fetch the winner
        # instead of bubbling a 500 that silently drops the command.
        return Cart.objects.get(customer=customer, status=Cart.Status.ACTIVE)


def add_item(customer, product_id, quantity=1):
    """Add (or bump) `product_id` x `quantity` on `customer`'s cart.

    Returns (cart, product) on success, or (None, error_string) if the
    product doesn't exist / is deleted, or the cart already holds the max
    number of distinct lines ('items_too_many'). Per-line quantity is clamped
    to the range [1, MAX_QUANTITY_PER_LINE] so a single /order can't request an
    unbounded amount.
    """
    if quantity < 1:
        quantity = 1
    try:
        product = Product.objects.get(id=product_id, is_deleted=False)
    except Product.DoesNotExist:
        return None, 'product_not_found'

    cart = get_or_create_active_cart(customer)
    with transaction.atomic():
        item, created = CartItem.objects.select_for_update().get_or_create(
            cart=cart, product=product,
            defaults={'quantity': min(quantity, MAX_QUANTITY_PER_LINE),
                      'price': product.price},
        )
        if created:
            # Reject a brand-new line once the cart is already at the line cap.
            # Re-check inside the lock so concurrent /order adds can't overshoot.
            if cart.items.count() > MAX_ITEMS_PER_ORDER:
                item.delete()
                return None, 'items_too_many'
        else:
            # Bump but never exceed the per-line cap.
            item.quantity = min(item.quantity + quantity, MAX_QUANTITY_PER_LINE)
            item.save(update_fields=['quantity', 'updated_at'])
    return cart, product


def remove_item(customer, product_id):
    cart = get_or_create_active_cart(customer)
    removed = CartItem.objects.filter(
        cart=cart, product_id=product_id,
    ).delete()
    return removed[0] > 0


def clear(customer):
    cart = get_or_create_active_cart(customer)
    CartItem.objects.filter(cart=cart).delete()
    return cart


def cart_total(cart):
    total = Decimal('0')
    for item in cart.items.select_related('product'):
        total += item.price * item.quantity
    return total


def _provision_user_for_customer(customer):
    """Get-or-create a base.User row to attach this customer's orders to."""
    if customer.user_id:
        return customer.user

    # Email must be unique. chat_id is unique per Telegram account; combine
    # with a domain that won't collide with real staff emails.
    email = f'tg-{customer.chat_id}@telegram.local'
    user, created = User.objects.get_or_create(
        email=email,
        defaults={
            'first_name': customer.first_name or 'Telegram',
            'last_name': customer.last_name or 'Customer',
            'role': User.RoleChoices.USER,
            'status': User.UserStatus.ACTIVE,
            # Unusable password — the Telegram-only user never logs into
            # the staff/admin API. We still need *something* in the column
            # so we set a random value the bcrypt verifier will reject.
            'password': secrets.token_hex(32),
        },
    )
    if created:
        logger.info('Provisioned base.User<%s> for TelegramCustomer<%s>',
                    user.id, customer.chat_id)
    customer.user = user
    customer.save(update_fields=['user'])
    return user


@transaction.atomic
def checkout(customer, phone_required=True):
    """Convert the active cart into a base.Order.

    Returns (order, None) on success, (None, error_code) on failure.
    error_code is one of: 'empty', 'no_phone'.
    """
    cart = get_or_create_active_cart(customer)
    # Lock the cart row before converting it. Two near-simultaneous checkouts
    # (trivially produced by double-tapping the "✅ Buyurtma" button) would
    # otherwise both read the same ACTIVE cart and create two full orders for
    # one cart. The loser blocks on this lock, then re-reads a non-ACTIVE
    # status and returns the order the winner already created.
    cart = Cart.objects.select_for_update().get(pk=cart.pk)
    if cart.status != Cart.Status.ACTIVE:
        if cart.order_id:
            return cart.order, None
        return None, 'empty'

    items = list(cart.items.select_related('product'))
    if not items:
        return None, 'empty'

    if phone_required and not customer.phone_number:
        return None, 'no_phone'

    user = _provision_user_for_customer(customer)
    total = cart_total(cart)

    # `Order.objects.count() + 1` raced under concurrent Telegram checkouts
    # and re-used display_ids; route through the DisplayIdCounter allocator
    # so kitchen-handoff numbers stay unique across surfaces.
    from base.repositories.order import OrderRepository
    order = Order.objects.create(
        user=user,
        phone_number=customer.phone_number or None,
        order_type=Order.OrderType.PICKUP,
        status=Order.Status.OPEN,
        is_paid=False,
        subtotal=total,
        total_amount=total,
        display_id=OrderRepository.next_display_id(),
        chef_queue_number=OrderRepository.next_chef_queue_number(),
        order_number=OrderRepository.next_order_number(),
    )
    for item in items:
        OrderItem.objects.create(
            order=order, product=item.product,
            quantity=item.quantity, price=item.price,
            original_price=item.price,
        )

    cart.status = Cart.Status.CHECKED_OUT
    cart.order = order
    cart.save(update_fields=['status', 'order', 'updated_at'])

    return order, None
