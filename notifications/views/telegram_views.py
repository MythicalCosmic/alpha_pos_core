"""Public-facing Telegram webhook.

Telegram POSTs every update (incoming message, button press, etc.) to the
URL configured via setWebhook. We authenticate the call by comparing the
`X-Telegram-Bot-Api-Secret-Token` header against settings.TELEGRAM_WEBHOOK_SECRET
— the same secret we register with setWebhook. Without that header we
return 401, no exceptions: this endpoint is publicly reachable and would
otherwise let anyone spoof updates.

The endpoint must respond 200 quickly even on internal errors; returning
non-200 makes Telegram retry the update for hours and floods the queue.
"""
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def webhook(request):
    expected = getattr(settings, 'TELEGRAM_WEBHOOK_SECRET', '') or ''
    if not expected:
        # Refuse to serve when the operator hasn't set the secret. Better
        # to return 503 than to accept unauthenticated updates.
        return JsonResponse(
            {'success': False, 'message': 'Telegram webhook not configured'},
            status=503,
        )

    presented = request.META.get('HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN', '') or ''
    if not constant_time_compare(presented, expected):
        return JsonResponse(
            {'success': False, 'message': 'Invalid webhook secret'},
            status=401,
        )

    try:
        update = json.loads(request.body)
    except (ValueError, TypeError):
        # Telegram never sends invalid JSON; treat this as someone probing.
        # Still 200 — refusing would just make them retry.
        logger.warning('Telegram webhook received non-JSON body')
        return JsonResponse({'ok': True})

    # Handling an update makes blocking HTTPS calls back to Telegram
    # (sendMessage, etc., ~10s timeout each). On a multi-worker gunicorn +
    # Postgres deployment that can tie up workers under load, so the operator
    # can opt into offloading to a background thread via TELEGRAM_ASYNC_INBOUND.
    # It defaults OFF because the single-PC default runs on SQLite, where a
    # background writer thread contends with request threads ("database is
    # locked"). Inline handling stays correct and is what low-volume single-PC
    # installs want.
    try:
        if getattr(settings, 'TELEGRAM_ASYNC_INBOUND', False):
            from notifications.services.inbound_worker import enqueue_update
            if enqueue_update(update):
                return JsonResponse({'ok': True})
            # Queue full — fall through to inline handling.
        from notifications.services.telegram_bot import handle_update
        handle_update(update)
    except Exception:
        # Swallow + log: a handler bug must not make Telegram keep retrying
        # the same update for hours. The bug surfaces in our logs.
        logger.exception('Telegram bot handler crashed on update %s',
                         update.get('update_id'))

    return JsonResponse({'ok': True})
