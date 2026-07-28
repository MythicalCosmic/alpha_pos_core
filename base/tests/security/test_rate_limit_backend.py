from django.core.cache import cache
from django.http import JsonResponse
from django.test import RequestFactory, override_settings

from base.security.rate_limit import rate_limit


LOCMEM_CACHE = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'rate-limit-contract-test',
    },
}

DUMMY_CACHE = {
    'default': {
        'BACKEND': 'django.core.cache.backends.dummy.DummyCache',
    },
}


@override_settings(CACHES=LOCMEM_CACHE)
def test_locmem_rate_limit_counts_requests():
    cache.clear()
    view = rate_limit('backend-contract', 1, 60)(
        lambda _request: JsonResponse({'success': True}),
    )
    request = RequestFactory().get('/', REMOTE_ADDR='127.0.0.1')

    assert view(request).status_code == 200
    assert view(request).status_code == 429


@override_settings(CACHES=DUMMY_CACHE)
def test_dummy_cache_disables_rate_limit_without_crashing():
    calls = 0

    def target(_request):
        nonlocal calls
        calls += 1
        return JsonResponse({'success': True})

    view = rate_limit('dummy-contract', 1, 60)(target)
    request = RequestFactory().get('/', REMOTE_ADDR='127.0.0.1')

    assert view(request).status_code == 200
    assert view(request).status_code == 200
    assert calls == 2
