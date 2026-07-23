import json
import uuid
from datetime import timedelta

import pytest
from django.db import transaction
from django.db.models import QuerySet
from django.test import RequestFactory
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _changes(settings, *, branch_id, since=None, per_page=None):
    from base.services.sync import views

    settings.ALLOWED_BRANCH_TOKENS = []
    settings.BRANCH_TOKEN_MAP = {'commit-feed-token': branch_id}
    params = {}
    if since is not None:
        params['since'] = since.isoformat()
    if per_page is not None:
        params['per_page'] = str(per_page)
    request = RequestFactory().get(
        '/api/sync/changes',
        params,
        HTTP_AUTHORIZATION='Branch commit-feed-token',
        HTTP_X_BRANCH_ID=branch_id,
    )
    response = views.changes(request)
    assert response.status_code == 200
    return json.loads(response.content)


def test_cloud_save_publishes_only_its_exact_version_after_commit(
    settings, monkeypatch, django_capture_on_commit_callbacks,
):
    from base.models import Category

    settings.DEPLOYMENT_MODE = 'cloud'
    baseline = timezone.now() - timedelta(days=1)
    category = Category.objects.bulk_create([
        Category(
            name='Before',
            slug='commit-publication',
            branch_id='cloud',
            synced_at=baseline,
        ),
    ])[0]

    with django_capture_on_commit_callbacks(execute=False) as first_callbacks:
        with transaction.atomic():
            category.name = 'First committed version'
            category.save(update_fields=['name'])
            persisted = Category.objects.get(pk=category.pk)
            assert persisted.synced_at is None
            first_version = persisted.sync_version

    assert len(first_callbacks) == 1

    # A second content commit wins before the first publisher runs. The old
    # callback must not stamp/acknowledge this newer version.
    with django_capture_on_commit_callbacks(execute=False) as second_callbacks:
        with transaction.atomic():
            category.description = 'Newer committed version'
            category.save(update_fields=['description'])
            persisted = Category.objects.get(pk=category.pk)
            assert persisted.synced_at is None
            assert persisted.sync_version == first_version + 1

    assert len(second_callbacks) == 1
    first_callbacks[0]()
    persisted.refresh_from_db()
    assert persisted.synced_at is None

    published_at = timezone.now()
    monkeypatch.setattr(timezone, 'now', lambda: published_at)
    second_callbacks[0]()
    persisted.refresh_from_db()
    assert persisted.synced_at == published_at


def test_publisher_failure_does_not_fail_committed_cloud_save(
    settings, monkeypatch, django_capture_on_commit_callbacks,
):
    from base.models import Category

    settings.DEPLOYMENT_MODE = 'cloud'
    category = Category.objects.bulk_create([
        Category(
            name='Before publisher failure',
            slug='publisher-failure',
            branch_id='cloud',
            synced_at=timezone.now() - timedelta(days=1),
        ),
    ])[0]

    def fail_publication(_queryset, **_kwargs):
        raise RuntimeError('injected synced_at publication failure')

    monkeypatch.setattr(QuerySet, 'update', fail_publication)

    # execute=True models Django running this callback after the content commit.
    # Its robust registration must contain the publisher error rather than make
    # the already-successful save/receiver request look rolled back.
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        category.name = 'Content still committed'
        category.save(update_fields=['name'])

    assert len(callbacks) == 1
    category.refresh_from_db()
    assert category.name == 'Content still committed'
    assert category.synced_at is None

    # A failed publisher remains crash-safe and visible through the NULL lane.
    body = _changes(settings, branch_id='branch-a', per_page=1)
    returned = {
        row['uuid']
        for rows in body['data'].values()
        for row in rows
    }
    assert str(category.uuid) in returned


def test_cloud_receiver_publishes_after_commit_and_preserves_feed_scope_order(
    settings, monkeypatch, django_capture_on_commit_callbacks,
):
    from base.models import Customer
    from base.services.sync.receiver import CloudReceiver

    settings.DEPLOYMENT_MODE = 'cloud'
    base_time = timezone.now().replace(microsecond=0)
    older = Customer.objects.bulk_create([
        Customer(
            name='Older cloud row',
            branch_id='branch-a',
            synced_at=base_time,
        ),
    ])[0]
    received_uuid = uuid.uuid4()

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        received, action = CloudReceiver._create_or_update(
            Customer,
            {
                'uuid': str(received_uuid),
                'sync_version': 7,
                'name': 'Received branch row',
            },
            'branch-a',
        )
        assert action == 'created'
        assert Customer.objects.get(pk=received.pk).synced_at is None

    assert len(callbacks) == 1
    published_at = base_time + timedelta(seconds=1)
    monkeypatch.setattr(timezone, 'now', lambda: published_at)
    callbacks[0]()
    received.refresh_from_db()
    assert received.synced_at == published_at
    assert received.branch_id == 'branch-a'

    snapshot = base_time + timedelta(seconds=2)
    monkeypatch.setattr(timezone, 'now', lambda: snapshot)
    other_feed = _changes(settings, branch_id='branch-b')
    customer_uuids = [
        row['uuid'] for row in other_feed['data'].get('customer', [])
    ]
    assert str(older.uuid) not in customer_uuids
    assert str(received_uuid) not in customer_uuids

    source_feed = _changes(settings, branch_id='branch-a')
    source_customer_uuids = [
        row['uuid'] for row in source_feed['data']['customer']
    ]
    assert source_customer_uuids == [str(older.uuid), str(received_uuid)]


def test_null_rows_are_promoted_in_bounded_slices_on_successive_pulls(
    settings, monkeypatch,
):
    from base.models import Category

    settings.DEPLOYMENT_MODE = 'cloud'
    null_rows = Category.objects.bulk_create([
        Category(
            name=f'Crash-safe row {index}',
            slug=f'crash-safe-row-{index}',
            branch_id='cloud',
            synced_at=None,
        )
        for index in range(7)
    ])
    expected = {str(row.uuid) for row in null_rows}
    snapshot = timezone.now().replace(microsecond=0)
    monkeypatch.setattr(timezone, 'now', lambda: snapshot)

    bootstrap = _changes(settings, branch_id='branch-a', per_page=2)
    bootstrap_ids = {row['uuid'] for row in bootstrap['data']['category']}
    assert len(bootstrap_ids) == 2
    assert bootstrap_ids < expected
    assert Category.objects.filter(synced_at__isnull=True).count() == 5
    assert bootstrap['has_more'] is False
    assert bootstrap['next_since'] is None

    cursored = _changes(
        settings,
        branch_id='branch-a',
        since=snapshot - timedelta(hours=1),
        per_page=2,
    )
    cursored_ids = {row['uuid'] for row in cursored['data']['category']}
    assert bootstrap_ids <= cursored_ids < expected
    # Exactly one additional NULL slice was promoted. Frozen-time timestamp
    # ties may make the response replay the prior slice too, but never promote
    # the unbounded remainder in one request.
    assert Category.objects.filter(synced_at__isnull=True).count() == 3
