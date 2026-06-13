"""Webhook for the customer-facing Telegram bot (server edition).

Separate from the staff webhook (different bot, different token + secret). Telegram
POSTs every update here; we auth via the X-Telegram-Bot-Api-Secret-Token header
(set when registering setWebhook), then greet + offer the web app. Always returns
200 quickly — a non-200 makes Telegram retry for hours.
"""
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from notifications.services import customer_bot

logger = logging.getLogger('notifications.customer_bot')


@csrf_exempt
@require_POST
def customer_webhook(request):
    expected = getattr(settings, 'CUSTOMER_WEBHOOK_SECRET', '') or ''
    if not expected:
        return JsonResponse(
            {'success': False, 'message': 'Customer bot not configured'},
            status=503,
        )
    presented = request.META.get('HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN', '') or ''
    if not constant_time_compare(presented, expected):
        return JsonResponse({'success': False, 'message': 'Invalid webhook secret'},
                            status=401)
    try:
        update = json.loads(request.body or b'{}')
        if isinstance(update, dict):
            customer_bot.handle_update(update)
    except Exception:  # noqa: BLE001 — must still ack 200 so Telegram stops retrying
        logger.exception('customer bot webhook: failed to process update')
    return JsonResponse({'ok': True})
