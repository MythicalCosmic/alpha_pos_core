"""Public QR self-order service.

Each table gets a permanent signed URL (printed once on a sticker). The
URL carries a signed token that maps to a specific Table row; tampering
with the token fails signature verification, so a malicious customer
can't redirect their order to someone else's table.

Token format uses django.core.signing.Signer — HMAC-SHA256 over the
table UUID with SECRET_KEY (or QR_SIGNING_KEY if separately configured).
The token is opaque base64-ish text; we just round-trip it through
Signer.sign / unsign.

Order semantics:
  - order_type = HALL
  - table is the resolved table
  - user is a single auto-provisioned "qr-anonymous@alpha-pos.local"
    placeholder (not per-customer, no contact info collected)
  - status = OPEN, is_paid = False (the cashier closes + collects)
"""
import logging
import secrets
from decimal import Decimal

from django.conf import settings
from django.core.signing import BadSignature, Signer
from django.db import transaction

from base.models import Order, OrderItem, Product, Table, User
from base.repositories.order import OrderRepository

logger = logging.getLogger(__name__)

# Salt scopes the signature so a token signed for one purpose can't be
# replayed against a different feature that uses the same SECRET_KEY.
TOKEN_SALT = 'qr-order-v1'

# Hard cap on a single QR order — sanity bound so a malicious or buggy
# client can't create a 10,000-item order that bricks the cashier UI.
MAX_ITEMS_PER_ORDER = 50
MAX_QUANTITY_PER_LINE = 99


def _signer():
    key = getattr(settings, 'QR_SIGNING_KEY', '') or settings.SECRET_KEY
    return Signer(key=key, salt=TOKEN_SALT)


def make_token(table):
    """Build the signed URL fragment for `table`. Stable for the
    lifetime of the table (signature only changes if QR_SIGNING_KEY
    rotates), so a printed sticker keeps working."""
    return _signer().sign(str(table.uuid))


def resolve_token(token):
    """Return the active Table for `token`, or None if invalid."""
    try:
        uuid_str = _signer().unsign(token)
    except BadSignature:
        return None
    try:
        return Table.objects.get(uuid=uuid_str, is_active=True, is_deleted=False)
    except Table.DoesNotExist:
        return None


def _qr_user():
    """Singleton placeholder user for all QR orders. Generated lazily so a
    fresh deployment without seed data still works. Marked SUSPENDED so the
    auth-login path refuses it — there is no human owner of these
    credentials, so any login attempt against them is by definition
    illegitimate. Order rows still use this user as the FK target."""
    user, created = User.objects.get_or_create(
        email='qr-anonymous@alpha-pos.local',
        defaults={
            'first_name': 'QR', 'last_name': 'Customer',
            'role': User.RoleChoices.USER,
            'status': User.UserStatus.SUSPENDED,
            'password': secrets.token_hex(32),
        },
    )
    if created:
        logger.info('Provisioned QR placeholder user id=%s', user.id)
    return user


def validate_items(items):
    """Normalize + validate the incoming item list.

    Returns (rows, error). `rows` is a list of (product, quantity, price)
    tuples ready for OrderItem creation. `error` is None on success or a
    short string code on failure.
    """
    if not isinstance(items, list) or not items:
        return None, 'items_empty'
    if len(items) > MAX_ITEMS_PER_ORDER:
        return None, 'items_too_many'

    rows = []
    for entry in items:
        if not isinstance(entry, dict):
            return None, 'items_invalid'
        try:
            product_id = int(entry.get('product_id'))
            quantity = int(entry.get('quantity', 1))
        except (TypeError, ValueError):
            return None, 'items_invalid'
        if quantity < 1 or quantity > MAX_QUANTITY_PER_LINE:
            return None, 'quantity_out_of_range'

        try:
            product = Product.objects.get(id=product_id, is_deleted=False)
        except Product.DoesNotExist:
            return None, 'product_not_found'
        rows.append((product, quantity, product.price))
    return rows, None


@transaction.atomic
def create_qr_order(table, items, customer_note=None):
    """Place the order at `table` for the validated `items`."""
    user = _qr_user()
    total = Decimal('0')
    for _, qty, price in items:
        total += price * qty

    # `Order.objects.count() + 1` raced under concurrent QR scans and
    # silently re-used display_ids; route through the DisplayIdCounter
    # allocator that admins/waiters use so kitchen-handoff numbers stay
    # unique across surfaces (and wrap at DISPLAY_ID_WRAP_AT).
    order = Order.objects.create(
        user=user,
        place=table.place,
        table=table,
        order_type=Order.OrderType.HALL,
        status=Order.Status.OPEN,
        is_paid=False,
        subtotal=total,
        total_amount=total,
        description=(customer_note or '')[:500] or None,
        display_id=OrderRepository.next_display_id(),
        chef_queue_number=OrderRepository.next_chef_queue_number(),
    )
    for product, qty, price in items:
        OrderItem.objects.create(
            order=order, product=product, quantity=qty,
            price=price, original_price=price,
        )
    return order
