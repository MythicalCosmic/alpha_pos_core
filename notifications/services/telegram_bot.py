"""Inbound Telegram bot: dispatch incoming updates to command handlers.

Replies render through the editable NotificationTemplate system so a
restaurant can change the bot's wording from the admin UI without a deploy.
The template names this module uses are:

    telegram.start                — reply to /start
    telegram.unknown_command      — reply when we don't recognize the input
    telegram.menu_root            — reply to /menu (top-level category list)
    telegram.menu_category        — reply to /menu <slug> (products in category)
    telegram.menu_empty           — fallback when no categories are active
    telegram.menu_not_found       — fallback when slug doesn't match
    telegram.login_prompt         — reply to /login with the share-contact keyboard
    telegram.login_success        — confirmation after we save the phone
    telegram.login_other_contact  — sender shared someone else's contact card
    telegram.status_list          — reply to /status when we have orders
    telegram.status_empty         — reply to /status when none match
    telegram.status_unauthenticated — reply to /status before /login
    telegram.loyalty_balance      — reply to /loyalty showing stamp progress
    telegram.loyalty_unauthenticated — reply to /loyalty before /login
    telegram.loyalty_disabled     — reply when loyalty is turned off
    telegram.order_cart           — show current cart contents
    telegram.order_empty          — cart is empty
    telegram.order_added          — item added confirmation
    telegram.order_removed        — item removed confirmation
    telegram.order_cleared        — cart cleared confirmation
    telegram.order_checked_out    — order placed confirmation
    telegram.order_help           — /order usage help
    telegram.order_no_phone       — checkout needs /login first
    telegram.order_invalid_product — /order add unknown product id
"""
import datetime as _dt
import logging

from django.db.models import Count, Q
from django.utils import timezone

from base.models import Category, Order, Product
from base.notifications.telegram import TelegramAPI
from notifications.helpers import format_datetime, format_money
from notifications.models import NotificationTemplate, TelegramCustomer

logger = logging.getLogger(__name__)


# Registered command handlers. Add new commands here as they're built.
# Convention: lower-case, leading slash, no arguments in the key.
COMMAND_HANDLERS = {}


def register(command):
    """Decorator: register a handler under `/command`."""
    def decorator(fn):
        COMMAND_HANDLERS[command] = fn
        return fn
    return decorator


def handle_update(update):
    """Top-level entry point invoked from the webhook view.

    Dispatches `message` and `callback_query` updates. Inline queries and
    other update types are silently ignored.
    """
    # Idempotency: Telegram re-delivers an update whenever the webhook doesn't
    # ACK in time (and the async inbound path re-enqueues it too). Without
    # dedup, a redelivered '/order add' or an inc:/add: callback bumps the cart
    # twice. cache.add is atomic (sets only if absent) so concurrent
    # redeliveries collapse to one. update_id is unique per bot.
    update_id = update.get('update_id')
    if update_id is not None:
        from django.core.cache import cache
        if not cache.add(f'tg:update:{update_id}', True, 3600):
            return None

    callback = update.get('callback_query')
    if callback:
        return _handle_callback_query(callback)

    message = update.get('message') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    sender = message.get('from') or {}
    text = (message.get('text') or '').strip()

    if not chat_id:
        return None

    customer = _upsert_customer(chat_id, sender)

    if customer.is_blocked:
        # Telegram keeps trying to deliver some updates even after a block;
        # don't bother replying.
        return None

    # Contact share (from the request_contact button on /login) arrives as
    # a message with a `contact` payload and no command text. Handle it
    # before text routing so users don't have to type anything.
    contact = message.get('contact')
    if contact:
        return _handle_contact(customer, contact, sender)

    handler = _resolve_handler(text)
    return handler(customer, text)


def _resolve_handler(text):
    """Find the registered handler for `text`, or fall back to unknown."""
    if not text.startswith('/'):
        return _handle_unknown
    # Strip arguments — "/start abc" → "/start". Bot-suffixed commands
    # (Telegram appends @bot_username when used in groups) are normalized
    # the same way: "/start@my_bot" → "/start".
    head = text.split()[0]
    if '@' in head:
        head = head.split('@', 1)[0]
    return COMMAND_HANDLERS.get(head, _handle_unknown)


def _upsert_customer(chat_id, sender):
    """Create or refresh the TelegramCustomer row for this chat."""
    defaults = {
        'first_name': (sender.get('first_name') or '')[:64],
        'last_name': (sender.get('last_name') or '')[:64],
        'username': (sender.get('username') or '')[:64],
        'language_code': (sender.get('language_code') or '')[:8],
    }
    customer, created = TelegramCustomer.objects.get_or_create(
        chat_id=chat_id, defaults=defaults,
    )
    if not created:
        # Refresh profile fields in case the user changed their handle.
        for field, value in defaults.items():
            if value:
                setattr(customer, field, value)
        customer.last_seen_at = timezone.now()
        customer.save(update_fields=['first_name', 'last_name', 'username',
                                     'language_code', 'last_seen_at'])
    return customer


def _send(customer, text, reply_markup=None):
    """Reply to `customer` and update is_blocked if Telegram says so."""
    ok, err = TelegramAPI.send_to_chat(customer.chat_id, text, reply_markup=reply_markup)
    if not ok and err and err.startswith('API 403'):
        customer.is_blocked = True
        customer.save(update_fields=['is_blocked'])
    return ok


def _render(template_type, context):
    """Pull a NotificationTemplate by type and render it with context.

    Mirrors SenderService.send so behavior matches what staff notifications
    do — same HTML escaping, same brand fallback. Returns None if the
    template isn't seeded; the caller should handle that gracefully.
    """
    from notifications.services.sender_service import _escape_context
    from notifications.services.safe_format import safe_format, _UnsafePlaceholder
    from notifications.models import NotificationSettings

    template = NotificationTemplate.objects.filter(
        notification_type=template_type, is_enabled=True,
    ).first()
    if not template:
        return None

    settings = NotificationSettings.load()
    context.setdefault('brand', settings.brand_name)

    try:
        return safe_format(template.template_text, **_escape_context(context))
    except _UnsafePlaceholder as e:
        # Stored template reached into an object attribute / index. Drop
        # the reply and log loudly — admin needs to fix the template.
        logger.error('unsafe placeholder in template %s: %s', template_type, e)
        return None
    except (KeyError, IndexError, ValueError) as e:
        logger.error('Template render error for %s: %s', template_type, e)
        return None


# ---- Command handlers ------------------------------------------------------

@register('/start')
def _handle_start(customer, text):
    rendered = _render('telegram.start', {
        'first_name': customer.first_name or 'friend',
    })
    if rendered is None:
        # Safe fallback if the template was deleted by an over-zealous admin.
        rendered = 'Welcome.'
    return _send(customer, rendered)


def _handle_unknown(customer, text):
    rendered = _render('telegram.unknown_command', {
        'first_name': customer.first_name or 'friend',
        'input': text,
    })
    if rendered is None:
        rendered = "Sorry, I don't recognize that command yet."
    return _send(customer, rendered)


@register('/menu')
def _handle_menu(customer, text):
    """Show the menu.

    Without args: list top-level active categories with item counts.
    With `<slug>`: list that category's products and any subcategories.

    Categories are filtered through the SyncManager's `active()` (excludes
    soft-deleted rows) and the explicit ACTIVE status. Products use the
    default manager and we filter is_deleted in the query.
    """
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ''

    if not arg:
        return _send(customer, _render_menu_root(customer))
    text, keyboard = _render_menu_category(customer, arg)
    return _send(customer, text, reply_markup=keyboard)


def _render_menu_root(customer):
    categories = (
        Category.objects.active()
        .filter(parent__isnull=True, status='ACTIVE')
        .annotate(product_count=Count(
            'products', filter=Q(products__is_deleted=False),
        ))
        .order_by('sort_order', 'name')
    )
    if not categories.exists():
        rendered = _render('telegram.menu_empty', {
            'first_name': customer.first_name or 'friend',
        })
        return rendered or 'No menu items are available right now.'

    # Plain text only — the dispatcher's _escape_context HTML-escapes any
    # string value passed in the template context, so inline <b> would
    # render as literal "&lt;b&gt;" markup. Bold lives in the static
    # template_text wrapper instead.
    lines = []
    for cat in categories:
        lines.append(f'• {cat.name} ({cat.product_count}) — /menu {cat.slug}')

    rendered = _render('telegram.menu_root', {
        'first_name': customer.first_name or 'friend',
        'categories_list': '\n'.join(lines),
    })
    return rendered or '\n'.join(lines)


def _render_menu_category(customer, slug):
    try:
        category = Category.objects.active().get(slug=slug, status='ACTIVE')
    except Category.DoesNotExist:
        rendered = _render('telegram.menu_not_found', {'slug': slug})
        return rendered or f"No category '{slug}'.", None

    products = list(
        Product.objects.filter(category=category, is_deleted=False)
        .order_by('name')
    )
    product_lines = [
        f"• {p.name} — {format_money(p.price)} so'm"
        for p in products
    ]
    subcategories = (
        Category.objects.active()
        .filter(parent=category, status='ACTIVE')
        .order_by('sort_order', 'name')
    )
    subcat_lines = [
        f'• {c.name} — /menu {c.slug}' for c in subcategories
    ]

    body_parts = []
    if product_lines:
        body_parts.append('\n'.join(product_lines))
    if subcat_lines:
        body_parts.append('\n'.join(subcat_lines))
    body = '\n\n'.join(body_parts) if body_parts else '(empty)'

    rendered = _render('telegram.menu_category', {
        'category_name': category.name,
        'products_list': body,
    }) or f'{category.name}\n{body}'

    return rendered, _menu_category_keyboard(products)


# ---- Phone linking (/login) ------------------------------------------------

# Telegram's request_contact buttons render as a custom keyboard. Tapping
# the button sends the user's *own* phone number — Telegram clients restrict
# request_contact to the sender's number, but we still verify the contact's
# user_id matches the sender before saving (a hand-crafted client can POST
# anything to the bot API, and we don't want a sender to bind their account
# to someone else's phone).
_LOGIN_KEYBOARD = {
    'keyboard': [[{'text': "📞 Raqamni ulashish", 'request_contact': True}]],
    'resize_keyboard': True,
    'one_time_keyboard': True,
}
_REMOVE_KEYBOARD = {'remove_keyboard': True}


@register('/login')
def _handle_login(customer, text):
    rendered = _render('telegram.login_prompt', {
        'first_name': customer.first_name or 'friend',
    })
    if rendered is None:
        rendered = 'Tap the button below to share your phone.'
    return _send(customer, rendered, reply_markup=_LOGIN_KEYBOARD)


def _handle_contact(customer, contact, sender):
    """Save the phone if the contact belongs to the sender, else warn."""
    contact_user_id = contact.get('user_id')
    sender_id = sender.get('id')
    if contact_user_id and sender_id and contact_user_id != sender_id:
        rendered = _render('telegram.login_other_contact', {
            'first_name': customer.first_name or 'friend',
        }) or "Please share your own phone, not someone else's."
        return _send(customer, rendered, reply_markup=_REMOVE_KEYBOARD)

    phone = (contact.get('phone_number') or '').strip()
    if not phone:
        return None
    # Telegram returns phone like "998901234567" (no leading '+'); normalize
    # so downstream order-matching can compare to whatever the cashier typed
    # at order time. We keep a leading '+' if Telegram included one.
    customer.phone_number = phone[:20]
    customer.save(update_fields=['phone_number'])

    rendered = _render('telegram.login_success', {
        'first_name': customer.first_name or 'friend',
        'phone': customer.phone_number,
    }) or f'Saved {customer.phone_number}.'
    return _send(customer, rendered, reply_markup=_REMOVE_KEYBOARD)


# ---- /status ---------------------------------------------------------------

_STATUS_LABELS = {
    'OPEN': 'Ochiq',
    'PREPARING': 'Tayyorlanmoqda',
    'READY': 'Tayyor',
    'COMPLETED': 'Yakunlangan',
    'CANCELED': 'Bekor qilingan',
}
_STATUS_WINDOW_DAYS = 30
_STATUS_LIMIT = 10


@register('/status')
def _handle_status(customer, text):
    if not customer.phone_number:
        rendered = _render('telegram.status_unauthenticated', {
            'first_name': customer.first_name or 'friend',
        }) or 'Please /login first.'
        return _send(customer, rendered)

    # Match exact phone in both with-+ and without-+ forms. The Telegram
    # client returns the phone with no leading '+', but cashiers often type
    # "+998..." at order time. Smarter normalization (strip all non-digits,
    # last-9-suffix) is a follow-up if a venue's data turns out to be
    # inconsistent — V1 keeps the lookup an exact, indexed match.
    phone = customer.phone_number
    candidates = {phone}
    if phone.startswith('+'):
        candidates.add(phone[1:])
    else:
        candidates.add('+' + phone)

    since = timezone.now() - _dt.timedelta(days=_STATUS_WINDOW_DAYS)
    orders = (
        Order.objects.filter(
            phone_number__in=candidates,
            is_deleted=False,
            created_at__gte=since,
        )
        .order_by('-created_at')[:_STATUS_LIMIT]
    )

    if not orders:
        rendered = _render('telegram.status_empty', {
            'first_name': customer.first_name or 'friend',
            'phone': phone,
        }) or 'No recent orders.'
        return _send(customer, rendered)

    lines = []
    for order in orders:
        date_str, time_str = format_datetime(order.created_at)
        label = _STATUS_LABELS.get(order.status, order.status)
        paid = '✓' if order.is_paid else '–'
        lines.append(
            f"#{order.display_id} · {label} · {paid} · "
            f"{format_money(order.total_amount)} so'm · {date_str} {time_str[:5]}"
        )

    rendered = _render('telegram.status_list', {
        'first_name': customer.first_name or 'friend',
        'orders_list': '\n'.join(lines),
    }) or '\n'.join(lines)
    return _send(customer, rendered)


# ---- /order ----------------------------------------------------------------

@register('/order')
def _handle_order(customer, text):
    """Manage the customer's cart.

      /order                       → show cart
      /order add <id> [qty]        → add item (default qty=1)
      /order remove <id>           → remove item
      /order clear                 → empty cart
      /order checkout              → place order from cart
      /order help                  → list these commands
    """
    from notifications.services import cart_service

    parts = text.split()
    sub = parts[1].lower() if len(parts) > 1 else ''

    if sub == 'add':
        return _order_add(customer, parts[2:])
    if sub == 'remove':
        return _order_remove(customer, parts[2:])
    if sub == 'clear':
        cart_service.clear(customer)
        rendered = _render('telegram.order_cleared', {
            'first_name': customer.first_name or 'friend',
        }) or 'Cart cleared.'
        return _send(customer, rendered)
    if sub == 'checkout':
        return _order_checkout(customer)
    if sub == 'help':
        return _send(customer, _order_help_text(customer))
    return _send(customer, _render_cart(customer),
                 reply_markup=_cart_keyboard(customer))


def _render_cart(customer):
    from notifications.services import cart_service
    cart = cart_service.get_or_create_active_cart(customer)
    items = list(cart.items.select_related('product'))
    if not items:
        rendered = _render('telegram.order_empty', {
            'first_name': customer.first_name or 'friend',
        }) or 'Your cart is empty.'
        return rendered

    lines = []
    for item in items:
        subtotal = item.price * item.quantity
        lines.append(
            f"• {item.product.name} x{item.quantity} — "
            f"{format_money(subtotal)} so'm"
        )
    total = cart_service.cart_total(cart)
    rendered = _render('telegram.order_cart', {
        'first_name': customer.first_name or 'friend',
        'items_list': '\n'.join(lines),
        'total': format_money(total),
    }) or '\n'.join(lines) + f"\n\nTotal: {format_money(total)} so'm"
    return rendered


def _order_add(customer, args):
    from notifications.services import cart_service
    if not args:
        return _send(customer, _order_help_text(customer))
    try:
        product_id = int(args[0])
        qty = int(args[1]) if len(args) > 1 else 1
    except (ValueError, IndexError):
        return _send(customer, _order_help_text(customer))

    cart, result = cart_service.add_item(customer, product_id, qty)
    if cart is None:
        rendered = _render('telegram.order_invalid_product', {
            'product_id': product_id,
        }) or f'No product with id {product_id}.'
        return _send(customer, rendered)

    rendered = _render('telegram.order_added', {
        'product_name': result.name,
        'quantity': qty,
    }) or f'Added {result.name} x{qty}.'
    return _send(customer, rendered + '\n\n' + _render_cart(customer))


def _order_remove(customer, args):
    from notifications.services import cart_service
    if not args:
        return _send(customer, _order_help_text(customer))
    try:
        product_id = int(args[0])
    except ValueError:
        return _send(customer, _order_help_text(customer))
    removed = cart_service.remove_item(customer, product_id)
    if not removed:
        return _send(customer, _render_cart(customer))
    rendered = _render('telegram.order_removed', {
        'product_id': product_id,
    }) or 'Removed.'
    return _send(customer, rendered + '\n\n' + _render_cart(customer))


def _order_checkout(customer):
    from notifications.services import cart_service
    order, err = cart_service.checkout(customer)
    if err == 'empty':
        return _send(customer, _render('telegram.order_empty', {
            'first_name': customer.first_name or 'friend',
        }) or 'Your cart is empty.')
    if err == 'no_phone':
        rendered = _render('telegram.order_no_phone', {
            'first_name': customer.first_name or 'friend',
        }) or 'Please /login before checkout.'
        return _send(customer, rendered)

    rendered = _render('telegram.order_checked_out', {
        'first_name': customer.first_name or 'friend',
        'display_id': order.display_id,
        'total': format_money(order.total_amount),
    }) or f"Order #{order.display_id} placed. Total: {format_money(order.total_amount)} so'm."
    return _send(customer, rendered)


def _order_help_text(customer):
    rendered = _render('telegram.order_help', {
        'first_name': customer.first_name or 'friend',
    })
    if rendered:
        return rendered
    return (
        '/order — show cart\n'
        '/order add <id> [qty] — add item\n'
        '/order remove <id> — remove item\n'
        '/order clear — empty cart\n'
        '/order checkout — place order'
    )


# ---- /loyalty --------------------------------------------------------------

@register('/loyalty')
def _handle_loyalty(customer, text):
    from notifications.models import LoyaltySettings
    from notifications.services import loyalty_service

    settings = LoyaltySettings.load()
    if not settings.is_enabled:
        rendered = _render('telegram.loyalty_disabled', {
            'first_name': customer.first_name or 'friend',
        }) or 'Loyalty is currently disabled.'
        return _send(customer, rendered)

    if not customer.phone_number:
        rendered = _render('telegram.loyalty_unauthenticated', {
            'first_name': customer.first_name or 'friend',
        }) or 'Please /login first.'
        return _send(customer, rendered)

    account = loyalty_service.get_account(customer.phone_number)
    balance = account.stamps_balance if account else 0
    threshold = settings.stamps_per_reward
    remaining = max(threshold - balance, 0)
    available_rewards = balance // threshold if threshold > 0 else 0

    rendered = _render('telegram.loyalty_balance', {
        'first_name': customer.first_name or 'friend',
        'stamps': balance,
        'threshold': threshold,
        'remaining': remaining,
        'available_rewards': available_rewards,
        'reward': settings.reward_description,
    }) or (
        f'You have {balance} stamps. '
        f'{remaining} more for {settings.reward_description}.'
    )
    return _send(customer, rendered)


# ---- Callback queries (inline keyboards) -----------------------------------

# Same registration pattern as /commands. Handlers are keyed by the first
# colon-delimited part of `callback_data` ("inc:42" → handler "inc"). Tap
# handlers should always answer_callback_query to dismiss the spinner and
# typically call edit_message_text to update the in-place view.
CALLBACK_HANDLERS = {}


def register_callback(prefix):
    def decorator(fn):
        CALLBACK_HANDLERS[prefix] = fn
        return fn
    return decorator


def _handle_callback_query(callback):
    """Route a callback_query update to the right handler.

    `callback` is the raw dict from Telegram. We always answer (even on
    error / unknown data) so the user's spinner doesn't hang.
    """
    callback_id = callback.get('id')
    sender = callback.get('from') or {}
    message = callback.get('message') or {}
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    message_id = message.get('message_id')
    data = callback.get('data') or ''

    if not chat_id or not message_id:
        if callback_id:
            TelegramAPI.answer_callback_query(callback_id)
        return None

    customer = _upsert_customer(chat_id, sender)
    if customer.is_blocked:
        TelegramAPI.answer_callback_query(callback_id)
        return None

    prefix = data.split(':', 1)[0] if data else ''
    handler = CALLBACK_HANDLERS.get(prefix)
    if not handler:
        TelegramAPI.answer_callback_query(callback_id)
        return None
    return handler(customer, callback_id, message_id, data)


def _edit(customer, message_id, text, reply_markup=None):
    ok, err = TelegramAPI.edit_message_text(
        customer.chat_id, message_id, text, reply_markup=reply_markup,
    )
    if not ok and err and err.startswith('API 403'):
        customer.is_blocked = True
        customer.save(update_fields=['is_blocked'])
    return ok


@register_callback('add')
def _cb_add(customer, callback_id, message_id, data):
    """callback_data = "add:<product_id>" — add 1 to cart from /menu."""
    from notifications.services import cart_service
    try:
        product_id = int(data.split(':')[1])
    except (IndexError, ValueError):
        TelegramAPI.answer_callback_query(callback_id, 'Bad data')
        return None
    _cart, product = cart_service.add_item(customer, product_id, 1)
    if _cart is None:
        TelegramAPI.answer_callback_query(callback_id, 'Product not available')
        return None
    TelegramAPI.answer_callback_query(callback_id, f'+ {product.name}')
    # Don't edit /menu (the user may keep browsing). Send a brief cart
    # status as a new message so they know it landed.
    return _send(customer, _render_cart(customer),
                 reply_markup=_cart_keyboard(customer))


@register_callback('inc')
def _cb_inc(customer, callback_id, message_id, data):
    from notifications.services import cart_service
    try:
        product_id = int(data.split(':')[1])
    except (IndexError, ValueError):
        TelegramAPI.answer_callback_query(callback_id)
        return None
    cart_service.add_item(customer, product_id, 1)
    TelegramAPI.answer_callback_query(callback_id)
    return _edit(customer, message_id, _render_cart(customer),
                 reply_markup=_cart_keyboard(customer))


@register_callback('dec')
def _cb_dec(customer, callback_id, message_id, data):
    """Decrement quantity. Removes the row if quantity drops to 0."""
    from notifications.models import CartItem
    from notifications.services import cart_service
    try:
        product_id = int(data.split(':')[1])
    except (IndexError, ValueError):
        TelegramAPI.answer_callback_query(callback_id)
        return None
    cart = cart_service.get_or_create_active_cart(customer)
    item = CartItem.objects.filter(cart=cart, product_id=product_id).first()
    if item:
        if item.quantity <= 1:
            item.delete()
        else:
            item.quantity -= 1
            item.save(update_fields=['quantity', 'updated_at'])
    TelegramAPI.answer_callback_query(callback_id)
    return _edit(customer, message_id, _render_cart(customer),
                 reply_markup=_cart_keyboard(customer))


@register_callback('rm')
def _cb_rm(customer, callback_id, message_id, data):
    from notifications.services import cart_service
    try:
        product_id = int(data.split(':')[1])
    except (IndexError, ValueError):
        TelegramAPI.answer_callback_query(callback_id)
        return None
    cart_service.remove_item(customer, product_id)
    TelegramAPI.answer_callback_query(callback_id)
    return _edit(customer, message_id, _render_cart(customer),
                 reply_markup=_cart_keyboard(customer))


@register_callback('clear')
def _cb_clear(customer, callback_id, message_id, data):
    from notifications.services import cart_service
    cart_service.clear(customer)
    TelegramAPI.answer_callback_query(callback_id, 'Cleared')
    return _edit(customer, message_id, _render_cart(customer),
                 reply_markup=_cart_keyboard(customer))


@register_callback('checkout')
def _cb_checkout(customer, callback_id, message_id, data):
    from notifications.services import cart_service
    order, err = cart_service.checkout(customer)
    if err == 'empty':
        TelegramAPI.answer_callback_query(callback_id, 'Cart is empty')
        return None
    if err == 'no_phone':
        TelegramAPI.answer_callback_query(callback_id, 'Use /login first')
        rendered = _render('telegram.order_no_phone', {
            'first_name': customer.first_name or 'friend',
        }) or 'Please /login before checkout.'
        return _send(customer, rendered)

    TelegramAPI.answer_callback_query(callback_id, f'Order #{order.display_id}')
    rendered = _render('telegram.order_checked_out', {
        'first_name': customer.first_name or 'friend',
        'display_id': order.display_id,
        'total': format_money(order.total_amount),
    }) or f"Order #{order.display_id} placed."
    # Replace the cart-with-buttons message with the confirmation (no kb).
    return _edit(customer, message_id, rendered, reply_markup=None)


# ---- Inline keyboard builders ----------------------------------------------

def _menu_category_keyboard(products):
    """One button per product labeled with name + price. Tapping adds 1
    to the cart. Telegram caps callback_data at 64 bytes — "add:<id>"
    stays well under that for any practical id range."""
    rows = []
    for p in products:
        label = f"+ {p.name} — {format_money(p.price)} so'm"
        # Trim to 64 chars max (Telegram button text limit is 64 chars in
        # practice; longer is silently truncated and breaks alignment).
        if len(label) > 60:
            label = label[:57] + '...'
        rows.append([{
            'text': label,
            'callback_data': f'add:{p.id}',
        }])
    return {'inline_keyboard': rows} if rows else None


def _cart_keyboard(customer):
    """Per-item -/+/× row plus Checkout/Clear at the bottom."""
    from notifications.services import cart_service
    cart = cart_service.get_or_create_active_cart(customer)
    items = list(cart.items.select_related('product')[:10])
    if not items:
        return None
    rows = []
    for item in items:
        rows.append([
            {'text': f'➖ {item.product.name[:24]}',
             'callback_data': f'dec:{item.product_id}'},
            {'text': f'x{item.quantity}', 'callback_data': f'inc:{item.product_id}'},
            {'text': '✕', 'callback_data': f'rm:{item.product_id}'},
        ])
    rows.append([
        {'text': '🧹 Tozalash', 'callback_data': 'clear'},
        {'text': '✅ Buyurtma', 'callback_data': 'checkout'},
    ])
    return {'inline_keyboard': rows}
