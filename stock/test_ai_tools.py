"""AIToolbox: the read-only data tools the Claude assistant calls to see every
order/shift/cashier/product/stock row in detail."""
import json

import pytest

from stock.services.ai_tools_service import AIToolbox


def test_tool_schemas_are_wellformed():
    names = set()
    for t in AIToolbox.TOOLS:
        assert t['name'] and t['description']
        assert t['input_schema']['type'] == 'object'
        names.add(t['name'])
    # The detail tools the "see everything" request hinges on must exist.
    assert {
        'get_order', 'list_orders', 'get_open_shifts', 'get_shift',
        'list_shifts', 'list_cashiers', 'get_cashier', 'list_products',
        'list_stock', 'sales_report', 'business_analytics',
    } <= names


@pytest.mark.django_db
def test_get_datetime_returns_json():
    out = json.loads(AIToolbox.execute('get_datetime', {}))
    assert 'now' in out and 'today' in out and 'timezone' in out


@pytest.mark.django_db
def test_get_overview_returns_json_on_empty_db():
    out = json.loads(AIToolbox.execute('get_overview', {}))
    assert 'today_sales' in out and 'open_shifts' in out and 'stock' in out
    assert out['open_shifts_count'] == 0


@pytest.mark.django_db
def test_unknown_tool_reports_error():
    out = json.loads(AIToolbox.execute('does_not_exist', {}))
    assert 'error' in out


@pytest.mark.django_db
def test_list_orders_empty_is_clean():
    out = json.loads(AIToolbox.execute('list_orders', {'date': '2099-01-01'}))
    assert out['total_matching'] == 0 and out['orders'] == []


@pytest.mark.django_db
def test_get_order_missing_reports_error():
    out = json.loads(AIToolbox.execute('get_order', {'order_id': 999999}))
    assert 'error' in out


@pytest.mark.django_db
def test_sales_report_defaults_to_a_range():
    out = json.loads(AIToolbox.execute('sales_report', {}))
    assert 'date_from' in out and 'date_to' in out and 'totals' in out
    assert out['totals']['orders'] == 0


@pytest.mark.django_db
def test_list_stock_and_products_empty():
    stock = json.loads(AIToolbox.execute('list_stock', {}))
    assert stock['items'] == []
    products = json.loads(AIToolbox.execute('list_products', {}))
    assert products['total_matching'] == 0


def test_clamp_helper():
    from stock.services.ai_tools_service import _clamp
    assert _clamp('5', 50, 1, 200) == 5
    assert _clamp(-3, 50, 1, 200) == 1       # negative floored to lo
    assert _clamp(9999, 50, 1, 200) == 200   # capped to hi
    assert _clamp(None, 50, 1, 200) == 50     # missing -> default
    assert _clamp('x', 50, 1, 200) == 50      # non-numeric -> default


def test_cap_analytics_truncates_long_lists():
    from stock.services.ai_tools_service import _cap_analytics, ANALYTICS_ITEM_CAP
    block = {'items': [{'i': i} for i in range(ANALYTICS_ITEM_CAP + 25)], 'summary': {'n': 1}}
    out = _cap_analytics(block)
    assert len(out['items']) == ANALYTICS_ITEM_CAP
    assert out['_truncated']['items'] == ANALYTICS_ITEM_CAP + 25
    assert out['summary'] == {'n': 1}          # summary untouched


@pytest.mark.django_db
def test_list_tools_report_total_and_offset():
    # Every list tool must be pageable (total_matching + offset) so "everything"
    # is reachable past the per-call cap.
    for tool in ('list_stock', 'list_products', 'list_shifts', 'list_cashiers'):
        out = json.loads(AIToolbox.execute(tool, {}))
        assert 'total_matching' in out, tool
        assert 'offset' in out, tool


@pytest.mark.django_db
def test_open_shifts_shape():
    out = json.loads(AIToolbox.execute('get_open_shifts', {}))
    assert out['open_shifts_count'] == 0 and out['returned'] == 0


@pytest.mark.django_db
def test_business_analytics_all_returns_blocks():
    out = json.loads(AIToolbox.execute('business_analytics', {'kind': 'all'}))
    assert set(out.keys()) >= {'abc', 'xyz', 'menu', 'profitability', 'inventory_health'}
