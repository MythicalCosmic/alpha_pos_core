import json
from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.utils import timezone


pytestmark = pytest.mark.django_db


def test_changes_cursor_never_advances_past_an_unread_row(settings, monkeypatch):
    """Rows newer than the response snapshot belong to the next pull.

    Before the snapshot cutoff was added, ``changes`` queried every model and
    only then called ``timezone.now()`` for ``server_timestamp``.  A concurrent
    commit in that interval could be omitted from the response but covered by
    its cursor, making it permanently invisible to the branch.
    """
    from base.models import Category
    from base.services.sync import views

    cutoff = timezone.now().replace(microsecond=0)
    category = Category.objects.create(name='Committed during snapshot')
    Category.objects.filter(pk=category.pk).update(
        branch_id='cloud', synced_at=cutoff + timedelta(seconds=1),
    )
    legacy = Category.objects.create(
        name='Legacy without a sync timestamp', branch_id='cloud',
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
    from base.models import Category
    from base.services.sync import views

    cutoff = timezone.now().replace(microsecond=123456)
    category = Category.objects.create(name='Published at cutoff')
    Category.objects.filter(pk=category.pk).update(
        branch_id='cloud', synced_at=cutoff,
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
