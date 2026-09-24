import json
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from base.services.sync.views import changes
from stock.models import StockItem, StockLevel, StockLocation, StockUnit

pytestmark = pytest.mark.django_db


def test_feed_relationship_queries_do_not_grow_with_rows(settings):
    settings.DEPLOYMENT_MODE = 'cloud'
    settings.BRANCH_TOKEN_MAP = {'audit-token': 'branch1'}
    settings.ALLOWED_BRANCH_TOKENS = []
    unit = StockUnit.objects.create(name='Audit pieces', short_name='apc',
                                    unit_type='COUNT', is_base_unit=True)
    item = StockItem.objects.create(name='Audit item', base_unit=unit, branch_id='branch1')
    for n in range(12):
        location = StockLocation.objects.create(name=f'Audit {n}', branch_id='branch1')
        StockLevel.objects.create(stock_item=item, location=location, quantity=n,
                                  branch_id='branch1')
    # Avoid publication effects in the measurement; test serialization only.
    StockLevel.objects.all().update(synced_at=timezone.now())
    request = RequestFactory().get('/api/sync/changes',
                                   HTTP_AUTHORIZATION='Branch audit-token',
                                   HTTP_X_BRANCH_ID='branch1')
    with patch('base.services.sync.config.SYNC_ORDER', ['stocklevel']), \
            patch('base.services.sync.config.get_all_models', return_value={'stocklevel': StockLevel}), \
            CaptureQueriesContext(connection) as queries:
        response = changes(request)
    assert response.status_code == 200
    assert len(json.loads(response.content)['data']['stocklevel']) == 12
    related_fetches = [
        query['sql'] for query in queries.captured_queries
        if 'FROM "stock_stockitem"' in query['sql'] or 'FROM "stock_stocklocation"' in query['sql']
    ]
    assert related_fetches == []
