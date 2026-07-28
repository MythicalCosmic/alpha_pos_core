import json
from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.utils import timezone
from django.utils.dateparse import parse_datetime


pytestmark = pytest.mark.django_db


def test_changes_cursor_never_advances_past_an_unread_row(settings, monkeypatch):
    """Rows newer than the response snapshot belong to the next pull.

    Before the snapshot cutoff was added, ``changes`` queried every model and
    only then called ``timezone.now()`` for ``server_timestamp``.  A concurrent
    commit in that interval could be omitted from the response but covered by
    its cursor, making it permanently invisible to the branch.
    """
    from base.models import Category, SyncState
    from base.services.sync.feed_clock import CLOUD_FEED_CLOCK_KEY
    from base.services.sync import views

    cutoff = timezone.now().replace(microsecond=0)
    category = Category.objects.create(name='Committed during snapshot')
    Category.objects.filter(pk=category.pk).update(
        branch_id='cloud', synced_at=cutoff + timedelta(seconds=1),
    )
    legacy = Category.objects.create(
        name='Legacy without a sync timestamp', branch_id='cloud',
    )
    SyncState.objects.create(
        key=CLOUD_FEED_CLOCK_KEY,
        value=(cutoff - timedelta(microseconds=1)).isoformat(),
    )

    settings.ALLOWED_BRANCH_TOKENS = ['test-branch-token']
    settings.ALLOWED_BRANCH_IDS = ['branch1']
    settings.BRANCH_TOKEN_MAP = {}
    monkeypatch.setattr(timezone, 'now', lambda: cutoff)

    request = RequestFactory().get(
        '/api/sync/changes',
        HTTP_AUTHORIZATION='Branch test-branch-token',
        HTTP_X_BRANCH_ID='branch1',
    )
    response = views.changes(request)
    body = json.loads(response.content)

    assert response.status_code == 200
    assert body['server_timestamp'] == (
        cutoff - timedelta(microseconds=1)
    ).isoformat()
    returned = {
        record['uuid']
        for records in body['data'].values()
        for record in records
    }
    assert str(category.uuid) not in returned
    assert str(legacy.uuid) in returned


def test_changes_terminal_cursor_replays_equal_cutoff_publication(
    settings, monkeypatch,
):
    """A row published at the exact cutoff remains visible next pull."""
    from base.models import Category, SyncState
    from base.services.sync.feed_clock import CLOUD_FEED_CLOCK_KEY
    from base.services.sync import views

    cutoff = timezone.now().replace(microsecond=123456)
    category = Category.objects.create(name='Published at cutoff')
    Category.objects.filter(pk=category.pk).update(
        branch_id='cloud', synced_at=cutoff,
    )
    SyncState.objects.create(
        key=CLOUD_FEED_CLOCK_KEY,
        value=(cutoff - timedelta(microseconds=1)).isoformat(),
    )

    settings.ALLOWED_BRANCH_TOKENS = ['test-branch-token']
    settings.ALLOWED_BRANCH_IDS = ['branch1']
    settings.BRANCH_TOKEN_MAP = {}
    monkeypatch.setattr(timezone, 'now', lambda: cutoff)

    # Simulate the first response having already queried Category before an
    # on_commit publisher stamps it with the exact cutoff. Its returned cursor
    # must remain just behind that timestamp.
    first_cursor = cutoff - timedelta(microseconds=1)
    request = RequestFactory().get(
        '/api/sync/changes',
        {'since': first_cursor.isoformat()},
        HTTP_AUTHORIZATION='Branch test-branch-token',
        HTTP_X_BRANCH_ID='branch1',
    )
    response = views.changes(request)
    body = json.loads(response.content)

    returned = {
        record['uuid']
        for records in body['data'].values()
        for record in records
    }
    assert response.status_code == 200
    assert str(category.uuid) in returned
    assert body['server_timestamp'] == first_cursor.isoformat()


def test_cloud_update_remains_visible_after_wall_clock_moves_backwards(
    settings, monkeypatch, django_capture_on_commit_callbacks,
):
    """The durable feed clock, rather than wall time, orders publications."""
    from base.models import Category
    from base.services.sync import views

    settings.DEPLOYMENT_MODE = 'cloud'
    settings.ALLOWED_BRANCH_TOKENS = []
    settings.BRANCH_TOKEN_MAP = {'rollback-safe-token': 'branch1'}

    forward_time = timezone.now().replace(microsecond=400000)
    category = Category.objects.bulk_create([
        Category(
            name='Before clock rollback',
            slug='before-clock-rollback',
            branch_id='cloud',
            synced_at=forward_time - timedelta(hours=1),
        ),
    ])[0]

    monkeypatch.setattr(timezone, 'now', lambda: forward_time)
    first_request = RequestFactory().get(
        '/api/sync/changes',
        HTTP_AUTHORIZATION='Branch rollback-safe-token',
        HTTP_X_BRANCH_ID='branch1',
    )
    first_response = views.changes(first_request)
    first_body = json.loads(first_response.content)
    first_cursor = parse_datetime(first_body['server_timestamp'])

    backwards_time = forward_time - timedelta(hours=6)
    monkeypatch.setattr(timezone, 'now', lambda: backwards_time)
    with django_capture_on_commit_callbacks(execute=True):
        category.name = 'Committed after clock rollback'
        category.save(update_fields=['name'])

    category.refresh_from_db()
    assert category.synced_at > first_cursor
    assert category.synced_at > forward_time

    second_request = RequestFactory().get(
        '/api/sync/changes',
        {'since': first_body['server_timestamp']},
        HTTP_AUTHORIZATION='Branch rollback-safe-token',
        HTTP_X_BRANCH_ID='branch1',
    )
    second_response = views.changes(second_request)
    second_body = json.loads(second_response.content)
    returned = {
        record['uuid']
        for records in second_body['data'].values()
        for record in records
    }

    assert second_response.status_code == 200
    assert str(category.uuid) in returned
    assert (
        parse_datetime(second_body['server_timestamp'])
        > first_cursor
    )


def test_remote_since_cannot_poison_shared_cloud_feed_clock(
    settings, monkeypatch,
):
    from base.models import Category, SyncState
    from base.services.sync import views
    from base.services.sync.feed_clock import CLOUD_FEED_CLOCK_KEY

    settings.ALLOWED_BRANCH_TOKENS = []
    settings.BRANCH_TOKEN_MAP = {'cursor-input-token': 'branch1'}
    server_now = timezone.now().replace(microsecond=500000)
    Category.objects.bulk_create([
        Category(
            name='Existing publication',
            slug='existing-publication',
            branch_id='cloud',
            synced_at=server_now - timedelta(minutes=1),
        ),
    ])
    monkeypatch.setattr(timezone, 'now', lambda: server_now)

    extreme_request = RequestFactory().get(
        '/api/sync/changes',
        {'since': '9999-12-31T23:59:59.999999+00:00'},
        HTTP_AUTHORIZATION='Branch cursor-input-token',
        HTTP_X_BRANCH_ID='branch1',
    )
    extreme_response = views.changes(extreme_request)
    extreme_body = json.loads(extreme_response.content)

    assert extreme_response.status_code == 200
    assert (
        parse_datetime(
            SyncState.objects.get(key=CLOUD_FEED_CLOCK_KEY).value,
        )
        == server_now
    )
    assert parse_datetime(extreme_body['server_timestamp']) < parse_datetime(
        '9999-12-31T23:59:59.999999+00:00',
    )

    malformed_request = RequestFactory().get(
        '/api/sync/changes',
        {'since': 'not-a-sync-cursor'},
        HTTP_AUTHORIZATION='Branch cursor-input-token',
        HTTP_X_BRANCH_ID='branch1',
    )
    malformed_response = views.changes(malformed_request)
    assert malformed_response.status_code == 400


def test_feed_clock_bootstraps_beyond_existing_publication_after_rollback(
    settings, monkeypatch,
):
    """Installing the logical clock must not strand already-published rows."""
    from base.models import Category
    from base.services.sync import views

    settings.ALLOWED_BRANCH_TOKENS = []
    settings.BRANCH_TOKEN_MAP = {'clock-bootstrap-token': 'branch1'}
    rolled_back_now = timezone.now().replace(microsecond=200000)
    prior_publication = rolled_back_now + timedelta(hours=4)
    category = Category.objects.bulk_create([
        Category(
            name='Published before logical clock rollout',
            slug='pre-logical-clock-publication',
            branch_id='cloud',
            synced_at=prior_publication,
        ),
    ])[0]
    monkeypatch.setattr(timezone, 'now', lambda: rolled_back_now)

    request = RequestFactory().get(
        '/api/sync/changes',
        HTTP_AUTHORIZATION='Branch clock-bootstrap-token',
        HTTP_X_BRANCH_ID='branch1',
    )
    response = views.changes(request)
    body = json.loads(response.content)
    returned = {
        record['uuid']
        for records in body['data'].values()
        for record in records
    }

    assert response.status_code == 200
    assert str(category.uuid) in returned
    assert parse_datetime(body['server_timestamp']) >= prior_publication
