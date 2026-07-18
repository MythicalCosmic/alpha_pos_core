"""Public AI-chat failure contract.

Provider details belong in logs. The HTTP client must always receive a safe,
chat-ready message for provider rate limits and all other AI failures.
"""
import json
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.test import RequestFactory

from base.services import llm
from stock.services.ai_assistant_service import (
    AI_PROVIDER_ERROR_MESSAGE,
    AI_PROVIDER_RATE_LIMIT_MESSAGE,
    AI_REQUEST_RATE_LIMIT_MESSAGE,
    AIStockAssistant,
)


def _provider_result(monkeypatch, error):
    """Run process_query without touching business data or a real provider."""
    monkeypatch.setattr(llm, 'can_use_tools', lambda: False)
    monkeypatch.setattr(llm, 'call_ai', lambda *args, **kwargs: (None, error))
    monkeypatch.setattr(AIStockAssistant, '_get_all_stock_data', lambda *args: {})
    monkeypatch.setattr(AIStockAssistant, '_get_sales_data', lambda *args: {})
    monkeypatch.setattr(AIStockAssistant, '_needs_analytics', lambda query: False)
    return AIStockAssistant.process_query('How are sales?')


@pytest.mark.parametrize('provider_error', [
    'Error code: 429 - rate_limit_error',
    'RESOURCE_EXHAUSTED: too many requests',
    '529: model overloaded',
    '503 UNAVAILABLE: high demand',
])
def test_provider_rate_limit_returns_exact_user_message(monkeypatch, provider_error):
    result = _provider_result(monkeypatch, provider_error)

    assert result['error'] == 'quota_exceeded'
    assert result['error_source'] == 'ai_provider'
    assert result['retryable'] is True
    assert result['response'] == AI_PROVIDER_RATE_LIMIT_MESSAGE
    assert result['message'] == AI_PROVIDER_RATE_LIMIT_MESSAGE
    assert provider_error not in json.dumps(result)


@pytest.mark.parametrize('provider_error', [
    'upstream connection reset; request_id=secret-provider-id',
    'Error code: 429 - insufficient_quota: check billing',
])
def test_generic_provider_error_returns_exact_safe_message(monkeypatch, provider_error):
    result = _provider_result(monkeypatch, provider_error)

    assert result['error'] == 'internal_error'
    assert result['error_source'] == 'ai_provider'
    assert result['retryable'] is True
    assert result['response'] == AI_PROVIDER_ERROR_MESSAGE
    assert result['message'] == AI_PROVIDER_ERROR_MESSAGE
    assert provider_error not in json.dumps(result)


def _unwrapped(view):
    while hasattr(view, '__wrapped__'):
        view = view.__wrapped__
    return view


def test_ai_query_copies_chat_response_to_standard_error_message(monkeypatch):
    """HTTP 429/503 handlers must see the same text as the chat bubble."""
    from stock.views import ai_views

    request = RequestFactory().post(
        '/stock/ai/query/',
        data=json.dumps({'query': 'hello'}),
        content_type='application/json',
    )
    request.user = SimpleNamespace(id=7)
    monkeypatch.setattr(llm, 'key_missing', lambda: False)
    monkeypatch.setattr(
        ai_views.AIChatService,
        'send',
        lambda **kwargs: {
            'success': False,
            'error': 'quota_exceeded',
            'response': AI_PROVIDER_RATE_LIMIT_MESSAGE,
        },
    )

    response = _unwrapped(ai_views.ai_query)(request)
    payload = json.loads(response.content)

    assert response.status_code == 429
    assert payload['response'] == AI_PROVIDER_RATE_LIMIT_MESSAGE
    assert payload['message'] == AI_PROVIDER_RATE_LIMIT_MESSAGE


def test_ai_endpoint_burst_limit_is_chat_ready():
    """The local 10/min guard also returns a usable assistant message."""
    from stock.views.ai_views import ai_query

    cache.clear()
    factory = RequestFactory()
    try:
        responses = [
            ai_query(factory.post(
                '/stock/ai/query/',
                data=json.dumps({'query': 'hello'}),
                content_type='application/json',
                REMOTE_ADDR='192.0.2.77',
            ))
            for _ in range(11)
        ]
        response = responses[-1]
        payload = json.loads(response.content)
        assert response.status_code == 429
        assert payload['error'] == 'rate_limited'
        assert payload['error_source'] == 'alpha_pos'
        assert payload['response'] == AI_REQUEST_RATE_LIMIT_MESSAGE
        assert payload['message'] == AI_REQUEST_RATE_LIMIT_MESSAGE
        assert response.headers['Retry-After']
    finally:
        cache.clear()


def test_provider_rate_limit_classifier_excludes_billing_and_bad_keys():
    assert llm.is_provider_rate_limited('429 RESOURCE_EXHAUSTED rate limit')
    assert not llm.is_provider_rate_limited(
        '429 insufficient_quota: exceeded your current quota; check billing'
    )
    assert not llm.is_provider_rate_limited('401 invalid_api_key')

