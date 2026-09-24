import pytest

from stock.models import StockCount
from stock.services.count_service import StockCountService
from .test_audit_regressions import make_batch

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('quantity', ['-1', 'NaN', 'Infinity', 'invalid'])
def test_invalid_observation_does_not_start_count(catalog, quantity):
    c = catalog
    result, status = StockCountService.create(c.source.pk, 'FULL', c.actor.pk)
    assert status == 201, result
    count = StockCount.objects.get(pk=result['data']['id'])
    item = count.items.get()
    result, status = StockCountService.record_count(count.pk, item.pk, quantity)
    assert status == 422, result
    count.refresh_from_db()
    item.refresh_from_db()
    assert count.status == 'DRAFT'
    assert item.counted_quantity is None


def test_invalid_reason_does_not_start_count(catalog):
    c = catalog
    result, _ = StockCountService.create(c.source.pk, 'FULL', c.actor.pk)
    count = StockCount.objects.get(pk=result['data']['id'])
    result, status = StockCountService.record_count(count.pk, count.items.get().pk, 5,
                                                    reason_code_id=999999)
    assert status == 404, result
    count.refresh_from_db()
    assert count.status == 'DRAFT'


def test_string_false_cannot_approve_adjustments(catalog):
    c = catalog
    count = StockCount.objects.create(count_number='AUDIT-COUNT', location=c.source,
                                      count_type='FULL', counted_by=c.actor,
                                      status='PENDING_APPROVAL', branch_id='branch1')
    result, status = StockCountService.approve(count.pk, c.actor.pk, apply_adjustments='false')
    assert status == 422, result
    count.refresh_from_db()
    assert count.status == 'PENDING_APPROVAL'


def test_expired_batch_remains_visible_to_physical_count(catalog):
    from datetime import timedelta
    from django.utils import timezone

    c = catalog
    c.config.track_batches = True
    c.config.save()
    c.item.track_batches = True
    c.item.save()
    batch = make_batch(c, expiry_date=timezone.localdate() - timedelta(days=1))
    result, status = StockCountService.create(c.source.pk, 'FULL', c.actor.pk)
    assert status == 201, result
    count = StockCount.objects.get(pk=result['data']['id'])
    assert count.items.get().batch_id == batch.pk
