import pytest


pytestmark = pytest.mark.django_db


def _configure_local(settings):
    settings.DEPLOYMENT_MODE = 'local'
    settings.BRANCH_ID = 'main'
    settings.SYNC_ENABLED = True
    settings.SYNC_PULL_ENABLED = True


def test_stale_pull_generation_cannot_erase_new_full_replay(settings):
    from base.services.sync.status import SyncStatus

    _configure_local(settings)
    SyncStatus.set_cursor('2026-07-25T10:00:00+00:00')
    cursor, stale_generation = SyncStatus.get_pull_checkpoint()
    assert cursor == '2026-07-25T10:00:00+00:00'

    assert SyncStatus.request_full_pull() is True
    assert SyncStatus.get_cursor() is None

    assert SyncStatus.publish_pull_cursor(
        '2026-07-25T11:00:00+00:00',
        stale_generation,
        completes_full_pull=True,
    ) is False
    assert SyncStatus.get_cursor() is None
    assert SyncStatus.get()['full_pull_completed_at'] is None


def test_pull_service_does_not_publish_cursor_after_midflight_replay_request(
    settings, monkeypatch,
):
    from base.services.sync import service as sync_service
    from base.services.sync.status import SyncStatus

    _configure_local(settings)
    SyncStatus.set_cursor('2026-07-25T10:00:00+00:00')
    monkeypatch.setattr(
        SyncStatus, 'ensure_scope_epoch', classmethod(lambda cls: False),
    )
    monkeypatch.setattr(
        SyncStatus, 'ensure_pull_contract_epoch',
        classmethod(lambda cls: False),
    )
    monkeypatch.setattr(sync_service, 'check_health', lambda: True)
    monkeypatch.setattr(sync_service, 'get_all_models', lambda: {})
    monkeypatch.setattr(
        sync_service.SyncService, '_acquire_lock',
        classmethod(lambda cls, name: 'owner-a'),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_renew_lock',
        classmethod(lambda cls, name, token: True),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_release_lock',
        classmethod(lambda cls, name, token=None: None),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_notify_pull_success',
        staticmethod(lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_notify_error',
        staticmethod(lambda *args, **kwargs: None),
    )

    def fetch_changes(*, since_timestamp):
        assert since_timestamp == '2026-07-25T10:00:00+00:00'
        # This is the exact race: the operator's durable rewind lands while an
        # older pull still owns the transport lease.
        assert SyncStatus.request_full_pull() is True
        return {
            'success': True,
            'data': {},
            'has_more': False,
            'server_timestamp': '2026-07-25T11:00:00+00:00',
        }

    monkeypatch.setattr(sync_service, 'fetch_changes', fetch_changes)

    result = sync_service.SyncService.pull_from_cloud()

    assert result['success'] is False
    assert result['replay_queued'] is True
    assert 'remains queued' in result['message']
    assert SyncStatus.get_cursor() is None
    assert SyncStatus.get()['full_pull_completed_at'] is None
    assert 'remains queued' in SyncStatus.get()['last_pull_error']


def test_full_pull_visibility_survives_cache_loss(settings):
    from base.services.sync.service import SyncService
    from base.services.sync.status import SyncStatus

    _configure_local(settings)
    SyncStatus.set_cursor('2026-07-25T10:00:00+00:00')

    assert SyncStatus.request_full_pull() is True
    requested = SyncService.get_status()
    request_generation = requested['full_pull_request_generation']
    assert requested['full_pull_pending'] is True
    assert requested['full_pull_state'] == 'pending'
    assert requested['full_pull_requested_at']
    assert requested['full_pull_completed_at'] is None

    # Simulate a Redis/LocMem flush or application restart.
    SyncStatus.clear()
    restarted = SyncService.get_status()
    assert restarted['full_pull_pending'] is True
    assert restarted['full_pull_state'] == 'pending'
    assert (
        restarted['full_pull_request_generation']
        == request_generation
    )
    assert restarted['full_pull_requested_at']

    assert SyncStatus.publish_pull_cursor(
        '2026-07-25T11:00:00+00:00',
        request_generation,
        completes_full_pull=True,
    ) is True
    SyncStatus.clear()

    completed = SyncService.get_status()
    assert completed['full_pull_pending'] is False
    assert completed['full_pull_state'] == 'completed'
    assert completed['full_pull_completed_at']
    assert (
        completed['full_pull_completed_generation']
        == request_generation
    )


def test_service_status_surfaces_background_pull_failure(settings):
    from base.services.sync.service import SyncService
    from base.services.sync.status import SyncStatus

    _configure_local(settings)
    SyncStatus.update(
        last_error=None,
        last_pull_at='2026-07-25T11:00:00+00:00',
        last_pull_error='Cloud feed rejected authorization',
    )

    status = SyncService.get_status()

    assert status['last_pull_at'] == '2026-07-25T11:00:00+00:00'
    assert status['last_pull_error'] == 'Cloud feed rejected authorization'
    assert status['last_error'] == 'Cloud feed rejected authorization'


def test_durable_replay_generation_overrides_late_stale_cache(settings):
    from base.services.sync.cache import safe_set
    from base.services.sync.service import SyncService
    from base.services.sync.status import STATUS_KEY, STATUS_TTL, SyncStatus

    _configure_local(settings)
    assert SyncStatus.request_full_pull() is True
    _, old_generation = SyncStatus.get_pull_checkpoint()
    assert SyncStatus.publish_pull_cursor(
        '2026-07-25T11:00:00+00:00',
        old_generation,
        completes_full_pull=True,
    ) is True

    assert SyncStatus.request_full_pull() is True
    _, new_generation = SyncStatus.get_pull_checkpoint()
    assert new_generation != old_generation

    # Simulate an older worker winning the cache write after the newer durable
    # request committed. The UI must still report the DB-backed generation.
    safe_set(STATUS_KEY, {
        'full_pull_requested_at': '2026-07-25T10:00:00+00:00',
        'full_pull_request_generation': old_generation,
        'full_pull_completed_at': '2026-07-25T11:00:00+00:00',
        'full_pull_completed_generation': old_generation,
    }, STATUS_TTL)

    status = SyncService.get_status()
    assert status['full_pull_pending'] is True
    assert status['full_pull_state'] == 'pending'
    assert status['full_pull_request_generation'] == new_generation
    assert status['full_pull_completed_at'] is None
    assert status['full_pull_completed_generation'] is None


@pytest.mark.parametrize(
    ('bad_model', 'sync_order', 'model_map'),
    [
        ('futuremodel', ['known'], {'known': object}),
        (
            'optionalmodel',
            ['known', 'optionalmodel'],
            {'known': object, 'optionalmodel': None},
        ),
    ],
)
def test_unknown_nonempty_feed_model_never_advances_cursor(
    settings,
    monkeypatch,
    bad_model,
    sync_order,
    model_map,
):
    from base.services.sync import service as sync_service
    from base.services.sync.status import SyncStatus

    _configure_local(settings)
    original_cursor = '2026-07-25T10:00:00+00:00'
    first_page_cursor = '2026-07-25T10:30:00+00:00'
    SyncStatus.set_cursor(original_cursor)
    monkeypatch.setattr(
        SyncStatus, 'ensure_scope_epoch', classmethod(lambda cls: False),
    )
    monkeypatch.setattr(
        SyncStatus, 'ensure_pull_contract_epoch',
        classmethod(lambda cls: False),
    )
    monkeypatch.setattr(sync_service, 'check_health', lambda: True)
    monkeypatch.setattr(sync_service, 'SYNC_ORDER', sync_order)
    monkeypatch.setattr(sync_service, 'get_all_models', lambda: model_map)
    monkeypatch.setattr(
        sync_service.SyncService, '_acquire_lock',
        classmethod(lambda cls, name: 'owner-a'),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_renew_lock',
        classmethod(lambda cls, name, token: True),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_release_lock',
        classmethod(lambda cls, name, token=None: None),
    )
    monkeypatch.setattr(
        sync_service.SyncService, '_notify_error',
        staticmethod(lambda *args, **kwargs: None),
    )
    apply_calls = []

    def apply_records(cls, model, records, **kwargs):
        apply_calls.append((model, records, kwargs))
        return {
            'created': 0,
            'updated': 0,
            'skipped': 0,
            'errors': [],
            'deferred': [],
        }

    monkeypatch.setattr(
        sync_service.SyncService,
        '_apply_records',
        classmethod(apply_records),
    )
    requested_cursors = []

    def fetch_changes(*, since_timestamp):
        requested_cursors.append(since_timestamp)
        if len(requested_cursors) == 1:
            return {
                'success': True,
                'data': {'known': [{'uuid': 'known-record'}]},
                'has_more': True,
                'next_since': first_page_cursor,
                'server_timestamp': first_page_cursor,
            }
        return {
            'success': True,
            'data': {bad_model: [{'uuid': 'unknown-record'}]},
            'has_more': False,
            'next_since': None,
            'server_timestamp': '2026-07-25T11:00:00+00:00',
        }

    monkeypatch.setattr(sync_service, 'fetch_changes', fetch_changes)

    result = sync_service.SyncService.pull_from_cloud()

    assert result['success'] is False
    assert result['schema_error'] is True
    assert bad_model in result['message']
    assert requested_cursors == [original_cursor, first_page_cursor]
    assert len(apply_calls) == 1
    # Even the safe first page is deliberately replayed: no part of a feed
    # containing an unknown nonempty model may become the durable checkpoint.
    assert SyncStatus.get_cursor() == original_cursor
    assert SyncStatus.get()['last_pull_error'] == result['message']
