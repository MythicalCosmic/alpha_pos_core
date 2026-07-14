"""Transport acknowledgements must be truthful at batch boundaries."""

from base.services.sync import transport


class _Response:
    status_code = 200
    text = ''

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _configure(settings):
    settings.CLOUD_SYNC_URL = 'https://cloud.test/sync'
    settings.CLOUD_SYNC_TOKEN = 'token'
    settings.BRANCH_ID = 'branch-a'
    settings.SYNC_MAX_RETRIES = 0


def test_partial_batch_with_only_skips_and_one_failure_is_not_whole_failure(
    settings, monkeypatch,
):
    _configure(settings)
    calls = []
    failed = 'bad-uuid'

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response({
            'success': True,
            'created': 0,
            'updated': 0,
            'skipped': 499,
            'errors': [f'{failed}: invalid'],
            'failed_uuids': [failed],
        })

    monkeypatch.setattr(transport.requests, 'post', post)
    result = transport.send_batch('order', [{'uuid': failed}])

    assert len(calls) == 1  # retry=0 is clamped to one real attempt
    assert result['success'] is True
    assert result['skipped'] == 499
    assert result['failed_uuids'] == [failed]


def test_fetch_retry_zero_still_issues_one_request(settings, monkeypatch):
    _configure(settings)
    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response({
            'data': {}, 'has_more': False,
            'server_timestamp': '2026-07-14T00:00:00+00:00',
        })

    monkeypatch.setattr(transport.requests, 'get', get)
    result = transport.fetch_changes()

    assert len(calls) == 1
    assert result['success'] is True


def test_legacy_errors_without_failed_ids_fail_closed(settings, monkeypatch):
    _configure(settings)
    monkeypatch.setattr(
        transport.requests, 'post',
        lambda *args, **kwargs: _Response({
            # Older/malformed receiver: it reports an error but gives the
            # client no UUIDs that are safe to retain.
            'created': 0,
            'updated': 0,
            'skipped': 2,
            'errors': ['one row was rejected'],
        }),
    )

    result = transport.send_batch('order', [{'uuid': 'unknown'}])

    assert result['success'] is False
    assert 'rejected' in result['error'].lower()
