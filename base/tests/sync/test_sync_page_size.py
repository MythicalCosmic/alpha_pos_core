from django.test import RequestFactory, override_settings

from base.services.sync.views import _sync_page_size


@override_settings(MAX_PER_PAGE=100)
def test_sync_page_size_uses_machine_feed_default_not_public_api_limit():
    request = RequestFactory().get('/api/sync/changes')

    assert _sync_page_size(request) == 1000


@override_settings(MAX_PER_PAGE=100)
def test_sync_page_size_has_its_own_bounded_limit():
    request = RequestFactory().get(
        '/api/sync/changes',
        {'per_page': '999999'},
    )

    assert _sync_page_size(request) == 5000


def test_sync_page_size_handles_invalid_and_nonpositive_values():
    invalid = RequestFactory().get(
        '/api/sync/changes',
        {'per_page': 'not-a-number'},
    )
    nonpositive = RequestFactory().get(
        '/api/sync/changes',
        {'per_page': '0'},
    )

    assert _sync_page_size(invalid) == 1000
    assert _sync_page_size(nonpositive) == 1
