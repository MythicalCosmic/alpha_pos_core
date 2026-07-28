import json
from types import SimpleNamespace

import pytest
from django.http import JsonResponse
from django.test import RequestFactory

from base.models import IdempotencyKey
from base.security.idempotency import idempotent


@pytest.mark.django_db
def test_same_client_key_cannot_replay_success_for_a_different_order():
    """A reused client key must not turn order B into a phantom-paid sale.

    Before resource-path scoping both requests shared one claim because the
    actor, view module and logical scope were identical.  The second request
    returned order A's cached 200 without invoking the payment view for B.
    """
    calls = []

    @idempotent('orders.pay')
    def pay_view(request, order_id):
        calls.append(order_id)
        return JsonResponse({
            'success': True,
            'data': {'order_id': order_id, 'is_paid': True},
        })

    factory = RequestFactory()

    def request(order_id):
        req = factory.post(
            f'/api/orders/{order_id}/pay',
            data='{}',
            content_type='application/json',
            HTTP_IDEMPOTENCY_KEY='pos-payment-attempt',
        )
        req.user = SimpleNamespace(id=17)
        return req

    first = pay_view(request(101), order_id=101)
    second = pay_view(request(102), order_id=102)
    replay = pay_view(request(102), order_id=102)

    assert first.status_code == 200
    assert second.status_code == 200
    assert replay.status_code == 200
    assert calls == [101, 102]
    assert IdempotencyKey.objects.count() == 2
    assert json.loads(first.content)['data']['order_id'] == 101
    assert json.loads(second.content)['data']['order_id'] == 102
    assert json.loads(replay.content)['data']['order_id'] == 102


@pytest.mark.django_db
def test_same_key_replays_only_the_exact_request_body_and_query():
    calls = []

    @idempotent('treasury.transfer')
    def transfer_view(request):
        calls.append(json.loads(request.body))
        return JsonResponse({'success': True, 'sequence': len(calls)})

    factory = RequestFactory()

    def request(body, query=''):
        path = '/api/treasury/transfer'
        if query:
            path += f'?{query}'
        req = factory.post(
            path,
            data=json.dumps(body),
            content_type='application/json',
            HTTP_IDEMPOTENCY_KEY='manager-transfer-key',
        )
        req.user = SimpleNamespace(id=23)
        return req

    first = transfer_view(request({'amount': '100000'}))
    exact_retry = transfer_view(request({'amount': '100000'}))
    changed_body = transfer_view(request({'amount': '200000'}))
    changed_query = transfer_view(
        request({'amount': '100000'}, query='branch=b2')
    )

    assert first.status_code == 200
    assert exact_retry.status_code == 200
    assert json.loads(exact_retry.content)['sequence'] == 1
    assert changed_body.status_code == 409
    assert changed_query.status_code == 409
    assert calls == [{'amount': '100000'}]
