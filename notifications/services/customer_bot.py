"""Customer-facing Telegram bot — minimal.

This is a SEPARATE bot from the staff/internal notifications bot (different token).
It is managed on the SERVER edition. Its only job: on /start or ANY message, greet
the customer in Uzbek and offer a button that opens the ordering web app (a Telegram
Web App / Mini App). All ordering happens in the web app — there is intentionally no
in-chat menu/cart logic here (that old flow is retired).

Config (settings, env-driven):
    CUSTOMER_BOT_TOKEN       BotFather token for the customer bot.
    CUSTOMER_WEBHOOK_SECRET  Shared secret echoed by Telegram (setWebhook secret_token).
    CUSTOMER_WEBAPP_URL      HTTPS URL of the ordering web app (any test site for now).
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger('notifications.customer_bot')

GREETING = 'Salom! 👋 Buyurtma berish uchun quyidagi tugmani bosing.'
BUTTON_TEXT = 'Menyuni ochish 🍽️'
# First contact: ask the customer to share their name + phone so their in-store
# orders + Smart Club loyalty follow them into the bot (we key the unified client
# on phone). Telegram's request_contact shares the phone AND the Telegram name.
ASK_CONTACT = ("Salom! 👋 Smart Club a'zoligingiz, buyurtmalaringiz va "
               "chegirmalaringiz uchun ismingiz va telefon raqamingizni ulashing.")
SHARE_BTN = '📱 Telefon raqamni ulashish'
THANKS = 'Rahmat! ✅ Endi menyuni ochishingiz mumkin.'
_API = 'https://api.telegram.org/bot{token}/sendMessage'


def _chat_id(update: dict):
    """Pull the chat id from any update that carries one (message, edited
    message, or a callback query). For a customer DM the chat id == telegram_id."""
    msg = update.get('message') or update.get('edited_message')
    if msg:
        return (msg.get('chat') or {}).get('id')
    cq = update.get('callback_query') or {}
    return ((cq.get('message') or {}).get('chat') or {}).get('id')


def _keyboard():
    url = getattr(settings, 'CUSTOMER_WEBAPP_URL', '') or 'https://example.com'
    # web_app button = opens the Telegram Mini App in-chat. (A plain `url` button
    # is the fallback if you ever point it at a non-Mini-App site.)
    return {'inline_keyboard': [[{'text': BUTTON_TEXT, 'web_app': {'url': url}}]]}


def _contact_keyboard():
    return {
        'keyboard': [[{'text': SHARE_BTN, 'request_contact': True}]],
        'resize_keyboard': True,
        'one_time_keyboard': True,
    }


def build_reply(chat_id) -> dict:
    """Greet + open-the-web-app payload (used once we already have a phone)."""
    return {'chat_id': chat_id, 'text': GREETING, 'reply_markup': _keyboard()}


def _capture_contact(update: dict, chat_id):
    """If the update carries a contact the user shared about THEMSELVES, persist
    name + phone onto the unified base.Customer (keyed by phone + telegram_id), so
    in-store history + loyalty link to this Telegram account. Returns the phone
    string if captured, else None. Never raises."""
    msg = update.get('message') or {}
    contact = msg.get('contact') or {}
    phone = contact.get('phone_number')
    if not phone:
        return None
    owner = contact.get('user_id')
    if owner and chat_id and owner != chat_id:   # only trust the sender's own number
        return None
    name = ' '.join(p for p in (contact.get('first_name'), contact.get('last_name')) if p).strip()
    try:
        from base.models import Customer
        Customer.resolve(phone=phone, telegram_id=chat_id, name=name)
    except Exception:  # noqa: BLE001 — best-effort; the webhook must still ack 200
        logger.exception('customer bot: contact resolve failed for chat %s', chat_id)
    return phone


def _has_phone(telegram_id) -> bool:
    """True once the unified base.Customer for this Telegram account has a phone."""
    try:
        from base.models import Customer
        return Customer.objects.filter(
            is_deleted=False, telegram_id=telegram_id).exclude(phone_number='').exists()
    except Exception:  # noqa: BLE001
        return False


def handle_update(update: dict) -> bool:
    """On any update with a chat: capture a shared contact, else open the web app
    if we already know the phone, else ask for name+phone. Best-effort; never
    raises (the webhook must 200)."""
    token = getattr(settings, 'CUSTOMER_BOT_TOKEN', '') or ''
    if not token:
        logger.debug('customer bot: CUSTOMER_BOT_TOKEN not set; ignoring update')
        return False
    chat_id = _chat_id(update)
    if not chat_id:
        return False
    try:
        if _capture_contact(update, chat_id):
            payload = {'chat_id': chat_id, 'text': THANKS, 'reply_markup': _keyboard()}
        elif _has_phone(chat_id):
            payload = build_reply(chat_id)
        else:
            payload = {'chat_id': chat_id, 'text': ASK_CONTACT, 'reply_markup': _contact_keyboard()}
        requests.post(_API.format(token=token), json=payload, timeout=10)
        return True
    except Exception:  # noqa: BLE001 — best-effort; the webhook still acks 200
        logger.exception('customer bot: send failed for chat %s', chat_id)
        return False
