import pytest

from base.helpers.request import coerce_quantity


@pytest.mark.parametrize('value', [2**31, str(2**31), float(2**31), '9' * 5000], ids=['int-overflow', 'text-overflow', 'float-overflow', 'long-digits'])
def test_quantity_outside_database_capacity_is_rejected(value):
    assert coerce_quantity(value) is None


@pytest.mark.parametrize('value', [2**31 - 1, str(2**31 - 1)])
def test_quantity_at_database_boundary_is_supported(value):
    assert coerce_quantity(value) == 2**31 - 1


@pytest.mark.parametrize('value', [True, False, 1.0, 1.5, 0, -1, 2**63, 'abc', {'id': 1}, [1]])
def test_invalid_database_identifiers_are_rejected(value):
    from base.helpers.request import coerce_positive_id
    assert coerce_positive_id(value) is None


@pytest.mark.parametrize('value, expected', [(1, 1), ('001', 1), (' 2 ', 2), (2**63 - 1, 2**63 - 1)])
def test_supported_database_identifiers_are_normalized(value, expected):
    from base.helpers.request import coerce_positive_id
    assert coerce_positive_id(value) == expected


def test_overlong_identifier_is_invalid_without_integer_conversion_error():
    from base.helpers.request import coerce_positive_id
    assert coerce_positive_id('9' * 5000) is None
