import json
from types import SimpleNamespace

import pytest
from django.http import JsonResponse
from django.test import RequestFactory

from base.security.idempotency import idempotent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize('body', [
    {'success': True, 'data': {'zz': 1, 'a': 2, 'amount': '10000.25'}},
    [{'zz': 1, 'a': 2}, {'total': '10000.25'}],
    None, False, 0, 'complete',
], ids=['nested-object', 'array', 'null', 'false', 'zero', 'string'])
def test_json_replay_preserves_body_bytes_after_database_round_trip(body):
    calls = []

    @idempotent('audit.json-replay')
    def endpoint(request):
        calls.append(True)
        response = JsonResponse(body, safe=False, status=201)
        response['X-Audit-First-Response'] = 'preserved'
        response['Content-Length'] = len(response.content)
        response.set_cookie('audit-cookie', 'test-only')
        return response

    def request():
        value = RequestFactory().post('/audit-json', data='{}', content_type='application/json', HTTP_IDEMPOTENCY_KEY='json-roundtrip')
        value.user = SimpleNamespace(id=1)
        return value

    first = endpoint(request())
    replay = endpoint(request())
    assert calls == [True]
    assert first.status_code == replay.status_code == 201
    assert json.loads(first.content) == json.loads(replay.content) == body
    assert first.content == replay.content
    assert int(first['Content-Length']) == len(first.content)
    assert first['X-Audit-First-Response'] == 'preserved'
    assert first.cookies['audit-cookie'].value == 'test-only'
