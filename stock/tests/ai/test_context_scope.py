import pytest

from stock.models import StockLocation
from stock.services.ai_context import resolve_ai_context


@pytest.mark.django_db
def test_cloud_uses_explicit_default_branch_without_location(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.CLOUD_DEFAULT_TARGET_BRANCH_ID = 'branch-a'

    context, error = resolve_ai_context()

    assert error is None
    assert context.location_id is None
    assert context.branch_id == 'branch-a'


@pytest.mark.django_db
def test_multibranch_cloud_without_default_fails_closed(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.CLOUD_DEFAULT_TARGET_BRANCH_ID = ''
    StockLocation.objects.create(
        name='Kitchen A',
        type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-a',
    )
    StockLocation.objects.create(
        name='Kitchen B',
        type=StockLocation.LocationType.KITCHEN,
        branch_id='branch-b',
    )

    context, error = resolve_ai_context()

    assert context is None
    assert 'location_id is required' in error
