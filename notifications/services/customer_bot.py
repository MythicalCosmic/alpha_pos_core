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
_API = 'https://api.telegram.org/bot{token}/sendMessage'


def _chat_id(update: dict):
    """Pull the chat id from any update that carries one (message, edited
    message, or a callback query)."""
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


def build_reply(chat_id) -> dict:
    """The exact sendMessage payload — split out so it's unit-testable without
    hitting the network."""
    return {
        'chat_id': chat_id,
        'text': GREETING,
        'reply_markup': _keyboard(),
    }


def handle_update(update: dict) -> bool:
    """Greet + offer the web app on ANY incoming update that has a chat. Returns
    True if a reply was sent. Best-effort: never raises (the webhook must 200)."""
    token = getattr(settings, 'CUSTOMER_BOT_TOKEN', '') or ''
    if not token:
        logger.debug('customer bot: CUSTOMER_BOT_TOKEN not set; ignoring update')
        return False
    chat_id = _chat_id(update)
    if not chat_id:
        return False
    try:
        requests.post(_API.format(token=token), json=build_reply(chat_id), timeout=10)
        return True
    except Exception:  # noqa: BLE001 — best-effort; the webhook still acks 200
        logger.exception('customer bot: send failed for chat %s', chat_id)
        return False
