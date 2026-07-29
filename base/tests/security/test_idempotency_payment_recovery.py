import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from django.http import JsonResponse
from django.test import RequestFactory
from django.utils import timezone

from base.models import IdempotencyKey
from base.security.idempotency import idempotent


def _pay_request(*, key=None):
    extra = {}
    if key is not None:
        extra['HTTP_IDEMPOTENCY_KEY'] = key
    request = RequestFactory().post(
        '/api/orders/41/pay',
        data=json.dumps({
            'payment_method': 'CASH',
            'discount_percent': 0,
        }),
        content_type='application/json',
        **extra,
    )
    request.user = SimpleNamespace(id=17)
    return request


@pytest.mark.django_db
def test_request_fallback_protects_legacy_pay_client_without_header():
    calls = []

    @idempotent(
        'orders.pay',
        fallback_key_from_request=True,
        expose_action_id=True,
    )
    def pay_view(request):
        calls.append(request.idempotency_action_id)
        return JsonResponse({
            'success': True,
            'data': {
                'is_paid': True,
                'payment_action_id': str(request.idempotency_action_id),
            },
        })

    first = pay_view(_pay_request())
    replay = pay_view(_pay_request())

    assert first.status_code == 200
    assert replay.status_code == 200
    assert len(calls) == 1
    assert isinstance(calls[0], UUID)
    assert json.loads(first.content) == json.loads(replay.content)

    claim = IdempotencyKey.objects.get()
    assert claim.key.startswith('auto:')
    assert len(claim.key) <= 128
    assert claim.response_status == 200


@pytest.mark.django_db
def test_request_fallback_does_not_make_precondition_failure_sticky():
    calls = []

    @idempotent(
        'orders.pay',
        fallback_key_from_request=True,
        expose_action_id=True,
    )
    def pay_view(request):
        calls.append(request.idempotency_action_id)
        if len(calls) == 1:
            return JsonResponse(
                {
                    'success': False,
                    'message': 'Cashier has no active shift.',
                },
                status=422,
            )
        return JsonResponse({
            'success': True,
            'data': {
                'is_paid': True,
                'payment_action_id': str(request.idempotency_action_id),
            },
        })

    precondition_failure = pay_view(_pay_request())
    assert precondition_failure.status_code == 422
    assert IdempotencyKey.objects.count() == 0

    after_shift_opened = pay_view(_pay_request())
    assert after_shift_opened.status_code == 200
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert IdempotencyKey.objects.get().response_status == 200


@pytest.mark.django_db
def test_exact_payment_retry_recovers_committed_action_before_zombie_ttl():
    calls = []
    committed_action = None

    @idempotent(
        'orders.pay',
        fallback_key_from_request=True,
        expose_action_id=True,
        recover_inflight_after_seconds=5,
    )
    def pay_view(request):
        nonlocal committed_action
        action_id = request.idempotency_action_id
        calls.append(action_id)
        if committed_action is None:
            committed_action = action_id
        assert action_id == committed_action
        return JsonResponse({
            'success': True,
            'data': {
                'is_paid': True,
                'payment_action_id': str(committed_action),
            },
        })

    original = pay_view(_pay_request(key='checkout-attempt-41'))
    original_body = json.loads(original.content)

    # Simulate a worker that committed the sale but died before caching the
    # response: the business action exists, while the HTTP claim is in-flight.
    claim = IdempotencyKey.objects.get()
    IdempotencyKey.objects.filter(pk=claim.pk).update(
        response_status=0,
        response_body={},
        created_at=timezone.now() - timedelta(seconds=10),
    )

    recovered = pay_view(_pay_request(key='checkout-attempt-41'))
    replay = pay_view(_pay_request(key='checkout-attempt-41'))

    assert recovered.status_code == 200
    assert json.loads(recovered.content) == original_body
    assert json.loads(replay.content) == original_body
    assert calls == [committed_action, committed_action]

    claim.refresh_from_db()
    assert claim.response_status == 200
    assert claim.response_body == original_body


@pytest.mark.django_db
def test_early_payment_retry_gets_bounded_retry_after():
    calls = []

    @idempotent(
        'orders.pay',
        fallback_key_from_request=True,
        expose_action_id=True,
        recover_inflight_after_seconds=5,
    )
    def pay_view(request):
        calls.append(request.idempotency_action_id)
        return JsonResponse({'success': True})

    pay_view(_pay_request(key='still-running'))
    claim = IdempotencyKey.objects.get()
    IdempotencyKey.objects.filter(pk=claim.pk).update(
        response_status=0,
        response_body={},
        created_at=timezone.now(),
    )

    retry = pay_view(_pay_request(key='still-running'))

    assert retry.status_code == 409
    assert 1 <= int(retry['Retry-After']) <= 5
    assert len(calls) == 1


@pytest.mark.django_db
def test_oversized_key_is_rejected_instead_of_running_unprotected():
    called = False

    @idempotent('orders.pay')
    def pay_view(request):
        nonlocal called
        called = True
        return JsonResponse({'success': True})

    response = pay_view(_pay_request(key='x' * 129))

    assert response.status_code == 400
    assert called is False
    assert IdempotencyKey.objects.count() == 0


@pytest.mark.django_db
def test_default_decorator_remains_compatible_without_header():
    calls = []

    @idempotent('orders.create')
    def create_view(request):
        calls.append(1)
        return JsonResponse({'success': True, 'sequence': len(calls)})

    first = create_view(_pay_request())
    second = create_view(_pay_request())

    assert json.loads(first.content)['sequence'] == 1
    assert json.loads(second.content)['sequence'] == 2
    assert IdempotencyKey.objects.count() == 0


def test_inflight_recovery_requires_a_business_action_id():
    with pytest.raises(
        ValueError,
        match='requires expose_action_id=True',
    ):
        idempotent(
            'orders.pay',
            recover_inflight_after_seconds=5,
        )
