from datetime import timedelta

import pytest
from django.utils import timezone

from base.models import IdempotencyKey
from base.security.idempotency import _try_take_over_stale_claim


@pytest.mark.django_db
def test_stale_claim_takeover_is_compare_and_swap():
    """A retry holding an old lease snapshot cannot steal a refreshed claim."""
    row = IdempotencyKey.objects.create(
        scope='tests:actor:pay',
        key='same-payment-request',
        response_status=0,
        response_body={},
    )
    stale_at = timezone.now() - timedelta(minutes=5)
    IdempotencyKey.objects.filter(pk=row.pk).update(created_at=stale_at)
    stale_snapshot = IdempotencyKey.objects.get(pk=row.pk)

    # Simulate a competing retry winning the takeover after this worker read
    # the stale row but before it attempted its conditional update.
    winner_at = timezone.now()
    IdempotencyKey.objects.filter(pk=row.pk).update(created_at=winner_at)

    assert _try_take_over_stale_claim(stale_snapshot) is False
    row.refresh_from_db()
    assert row.created_at == winner_at
    assert row.response_status == 0


@pytest.mark.django_db
def test_first_stale_retry_refreshes_the_claim_lease():
    row = IdempotencyKey.objects.create(
        scope='tests:actor:pay',
        key='abandoned-payment-request',
        response_status=0,
        response_body={},
    )
    stale_at = timezone.now() - timedelta(minutes=5)
    IdempotencyKey.objects.filter(pk=row.pk).update(created_at=stale_at)
    snapshot = IdempotencyKey.objects.get(pk=row.pk)

    assert _try_take_over_stale_claim(snapshot) is True
    row.refresh_from_db()
    assert row.created_at > stale_at
