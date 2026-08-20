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

_OPEN = {
    'uz': {
        'text': 'Xush kelibsiz! Menyuni ochib, buyurtma berishingiz mumkin.',
        'button': 'Menyuni ochish',
    },
    'ru': {
        'text': 'Добро пожаловать! Откройте меню, чтобы сделать заказ.',
        'button': 'Открыть меню',
    },
    'en': {
        'text': 'Welcome! Open the menu to place your order.',
        'button': 'Open menu',
    },
}
_CLOSED = {
    'uz': 'Hozircha buyurtma qabul qilinmayapti, lekin menyuni ko‘rishingiz mumkin.',
    'ru': 'Сейчас заказы не принимаются, но вы можете посмотреть меню.',
    'en': 'Ordering is closed right now, but you can still browse the menu.',
}
_SEND_API = 'https://api.telegram.org/bot{token}/sendMessage'
_EDIT_MARKUP_API = 'https://api.telegram.org/bot{token}/editMessageReplyMarkup'


def _chat_id(update: dict):
    """Pull the chat id from any update that carries one (message, edited
    message, or a callback query). For a customer DM the chat id == telegram_id."""
    msg = update.get('message') or update.get('edited_message')
    if msg:
        return (msg.get('chat') or {}).get('id')
    cq = update.get('callback_query') or {}
    return ((cq.get('message') or {}).get('chat') or {}).get('id')


def _keyboard(button_text):
    url = getattr(settings, 'CUSTOMER_WEBAPP_URL', '') or 'https://example.com'
    # web_app button = opens the Telegram Mini App in-chat. (A plain `url` button
    # is the fallback if you ever point it at a non-Mini-App site.)
    return {'inline_keyboard': [[{'text': button_text, 'web_app': {'url': url}}]]}


def _language(update):
    message = update.get('message') or update.get('edited_message') or {}
    sender = message.get('from') or (update.get('callback_query') or {}).get('from') or {}
    language = str(sender.get('language_code') or 'uz').lower()[:2]
    return language if language in _OPEN else 'uz'


def _ordering_enabled():
    """Read the Smart Food switch when this core module runs on the server."""
    try:
        from django.apps import apps

        config_model = apps.get_model('smartfood', 'BotConfig')
        return bool(config_model.load().enabled)
    except Exception:  # noqa: BLE001 — chat entry remains available during config faults
        # The shared core can also run in editions without Smart Food installed.
        logger.warning('customer bot: ordering switch unavailable; defaulting open',
                       exc_info=True)
        return True


def build_reply(chat_id, language='uz', enabled=None) -> dict:
    """Always offer the Mini App; closure changes copy, not browsing access."""
    language = language if language in _OPEN else 'uz'
    copy = _OPEN[language]
    if enabled is None:
        enabled = _ordering_enabled()
    return {
        'chat_id': chat_id,
        'text': copy['text'] if enabled else _CLOSED[language],
        'reply_markup': _keyboard(copy['button']),
    }


def _telegram_result(response):
    """Return a successful Bot API result, without making chat entry brittle."""
    if not getattr(response, 'ok', False):
        return None
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get('ok') is not True:
        return None
    return payload.get('result')


def send_webapp_entry(token, payload):
    """Clear the retired contact keyboard and leave one Web App message.

    Telegram reply keyboards persist across bot deployments. A message must carry
    ReplyKeyboardRemove before an inline keyboard can replace that old UI. We send
    the localized entry copy with the removal, then attach the Web App button to
    that same message. If Telegram does not return an editable message id, a normal
    inline message is the safe fallback.
    """
    clear_payload = {
        'chat_id': payload['chat_id'],
        'text': payload['text'],
        'reply_markup': {'remove_keyboard': True},
    }
    try:
        response = requests.post(
            _SEND_API.format(token=token),
            json=clear_payload,
            timeout=10,
        )
    except requests.RequestException:
        response = None
    result = _telegram_result(response)
    message_id = result.get('message_id') if isinstance(result, dict) else None
    if message_id:
        try:
            edit_response = requests.post(
                _EDIT_MARKUP_API.format(token=token),
                json={
                    'chat_id': payload['chat_id'],
                    'message_id': message_id,
                    'reply_markup': payload['reply_markup'],
                },
                timeout=10,
            )
        except requests.RequestException:
            edit_response = None
        if _telegram_result(edit_response) is not None:
            return True

    # A failed/opaque remove response must not hide the Mini App entry point.
    fallback_response = requests.post(
        _SEND_API.format(token=token),
        json=payload,
        timeout=10,
    )
    return _telegram_result(fallback_response) is not None


def mark_reachable(chat_id):
    """A confirmed bot reply is proof that this Telegram chat is reachable."""
    try:
        from django.apps import apps

        customer_model = apps.get_model('smartfood', 'Customer')
        customer_model.objects.filter(telegram_id=chat_id).update(
            telegram_reachable=True,
        )
    except Exception:  # noqa: BLE001 — Smart Food is optional in shared core
        logger.debug('customer bot: reachability update unavailable', exc_info=True)


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


def handle_update(update: dict, token=None) -> bool:
    """On any update with a chat, immediately offer the Mini App.

    A contact sent voluntarily is still reconciled for backwards compatibility,
    but chat entry never asks for or gates on phone sharing. Checkout owns the
    explicit first-name, last-name, phone, confirmation, and location contract.
    Best-effort; never raises because the webhook must acknowledge Telegram.
    """
    if token is None:
        try:
            from smartfood.credentials import customer_bot_token

            token = customer_bot_token()
        except (ImportError, RuntimeError):
            token = getattr(settings, 'CUSTOMER_BOT_TOKEN', '') or ''
    if not token:
        logger.debug('customer bot: CUSTOMER_BOT_TOKEN not set; ignoring update')
        return False
    chat_id = _chat_id(update)
    if not chat_id:
        return False
    try:
        _capture_contact(update, chat_id)
        payload = build_reply(chat_id, _language(update))
        delivered = send_webapp_entry(token, payload)
        if delivered:
            mark_reachable(chat_id)
        return delivered
    except Exception:  # noqa: BLE001 — best-effort; the webhook still acks 200
        logger.exception('customer bot: send failed for chat %s', chat_id)
        return False
