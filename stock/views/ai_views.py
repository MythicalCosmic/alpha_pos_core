from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from base.helpers.request import parse_json_body
from base.helpers.response import json_response, ServiceResponse
from base.security.rate_limit import rate_limit
from base.security.permissions import admin_required
from stock.services.ai_assistant_service import AIStockAssistant
from stock.services.ai_chat_service import AIChatService


# Each query runs a batch of heavy aggregate ORM queries AND a billable Gemini
# call. The in-service per-user daily quota caps total volume, but nothing
# bounded the *rate* — a single session could fire them as fast as the network
# allows. Cap to 10/min per IP; normal interactive use is far below that.
@csrf_exempt
@require_POST
@rate_limit('ai_query', 10, 60)
@admin_required
def ai_query(request):
    # The assistant calls the configured LLM provider (Claude by default, or
    # Gemini). Without that provider's key the SDK raises deep in the request,
    # surfacing as a 500 with no actionable message. Check the ACTIVE provider's
    # key (not a hardcoded GEMINI_API_KEY) and return 503 so the client can hide
    # the feature instead of showing a generic failure.
    from base.services.llm import key_missing
    if key_missing():
        return JsonResponse({
            'success': False,
            'message': 'AI assistant is not configured (LLM API key missing).',
        }, status=503)

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    query = (data.get('query') or '').strip()
    if not query:
        return json_response(ServiceResponse.validation_error(
            errors={'query': 'Query is required'},
        ))

    # Persisted, multi-turn: pass chat_id to continue a conversation (history is
    # replayed to the model), or omit it to start a new chat. The response carries
    # back chat_id so the client can keep the thread going.
    result = AIChatService.send(
        user_id=request.user.id,
        query=query,
        chat_id=data.get('chat_id'),
        location_id=data.get('location_id'),
    )
    # Map service-layer error codes to real HTTP status codes so clients can
    # switch on response.status instead of having to parse the body. Without
    # this every failure (rate limit, invalid query, quota exhausted) comes
    # back as a 200 with `success: False` buried in the JSON.
    error_code = (result or {}).get('error')
    status_map = {
        'rate_limited': 429,
        'quota_exceeded': 429,
        'invalid_query': 422,
        'query_too_long': 422,
        'no_api_key': 503,
        # Provider unavailable / SDK missing / unexpected error — a server-side
        # failure, so return 5xx (not a client 400) and the desktop panel + infra
        # alerting can tell an outage apart from a bad request.
        'internal_error': 503,
    }
    status = status_map.get(error_code, 200 if result.get('success') else 400)
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
@admin_required
def ai_suggestions(request):
    from django.db.models import F
    from django.utils import timezone
    from datetime import timedelta
    from stock.models import StockLevel, StockBatch, PurchaseOrder

    suggestions = []

    low_stock_count = StockLevel.objects.filter(
        quantity__lte=F('stock_item__reorder_point'),
        is_deleted=False,
    ).count()
    if low_stock_count > 0:
        suggestions.append({
            'query': 'Show low stock items',
            'reason': f'{low_stock_count} items below reorder level',
            'priority': 'high',
        })

    expiring_count = StockBatch.objects.filter(
        expiry_date__lte=timezone.now().date() + timedelta(days=7),
        expiry_date__gt=timezone.now().date(),
        current_quantity__gt=0,
        is_deleted=False,
    ).count()
    if expiring_count > 0:
        suggestions.append({
            'query': "What's expiring this week?",
            'reason': f'{expiring_count} batches expiring soon',
            'priority': 'high',
        })

    pending_count = PurchaseOrder.objects.filter(
        status__in=['SENT', 'CONFIRMED', 'PARTIAL'],
        is_deleted=False,
    ).count()
    if pending_count > 0:
        suggestions.append({
            'query': 'Show pending deliveries',
            'reason': f'{pending_count} orders waiting',
            'priority': 'medium',
        })

    suggestions.extend([
        {'query': 'Stock overview', 'reason': 'See inventory summary', 'priority': 'low'},
        {'query': 'Top 10 most used items this month', 'reason': 'Analyze consumption', 'priority': 'low'},
        {'query': 'Stock value by location', 'reason': 'Financial overview', 'priority': 'low'},
    ])

    return JsonResponse({'success': True, 'suggestions': suggestions[:6]})


@csrf_exempt
@require_GET
@admin_required
def ai_quick_actions(request):
    actions = [
        {'id': 'low_stock', 'label': 'Low Stock', 'icon': 'warning', 'query': 'Show low stock items'},
        {'id': 'expiring', 'label': 'Expiring', 'icon': 'clock', 'query': "What's expiring in 7 days?"},
        {'id': 'overview', 'label': 'Overview', 'icon': 'chart', 'query': 'Stock summary'},
        {'id': 'top_items', 'label': 'Top Items', 'icon': 'fire', 'query': 'Top 10 most used items'},
        {'id': 'pending', 'label': 'Pending POs', 'icon': 'truck', 'query': 'Show pending orders'},
        {'id': 'forecast', 'label': 'Forecast', 'icon': 'crystal-ball', 'query': 'When will items run out?'},
    ]
    return JsonResponse({'success': True, 'actions': actions})


# ── Chat history ──
# The assistant now keeps persisted, per-operator conversations. The client sends
# chat_id with /ai/query/ to continue a thread; these endpoints list/open/delete
# the saved chats. All admin-gated like the rest of the AI surface.

@csrf_exempt
@require_GET
@admin_required
def ai_chats(request):
    """List the signed-in operator's saved chats (most-recent first)."""
    return JsonResponse({'success': True, 'chats': AIChatService.list_chats(request.user.id)})


@csrf_exempt
@require_GET
@admin_required
def ai_chat_messages(request, chat_id):
    """The full message history of one chat (404 if it isn't the caller's)."""
    chat = AIChatService.get_chat(request.user.id, chat_id)
    if chat is None:
        return JsonResponse({'success': False, 'message': 'Chat not found'}, status=404)
    return JsonResponse({'success': True, 'chat': chat})


@csrf_exempt
@require_POST
@admin_required
def ai_chat_delete(request, chat_id):
    """Soft-delete a chat."""
    if not AIChatService.delete_chat(request.user.id, chat_id):
        return JsonResponse({'success': False, 'message': 'Chat not found'}, status=404)
    return JsonResponse({'success': True})


@csrf_exempt
@require_POST
@admin_required
def ai_chat_rename(request, chat_id):
    """Rename a chat (body: {"title": "..."})."""
    data, error = parse_json_body(request)
    if error:
        return json_response(error)
    title = ((data or {}).get('title') or '').strip()
    if not title:
        return JsonResponse({'success': False, 'message': 'Title is required'}, status=422)
    if not AIChatService.rename_chat(request.user.id, chat_id, title):
        return JsonResponse({'success': False, 'message': 'Chat not found'}, status=404)
    return JsonResponse({'success': True})


