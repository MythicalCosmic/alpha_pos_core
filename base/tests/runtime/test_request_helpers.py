import pytest

from base.helpers.request import coerce_quantity


@pytest.mark.parametrize(
    ('value', 'expected'),
    (
        (1, 1),
        (2.0, 2),
        (' 3 ', 3),
        (True, None),
        (False, None),
        (0, None),
        (-1, None),
        (2.5, None),
        ('0', None),
        ('-1', None),
        ('2.5', None),
        ('abc', None),
        ('²', None),
        ('１２', None),
        (None, None),
    ),
)
def test_coerce_quantity_accepts_only_positive_ascii_integers(value, expected):
    assert coerce_quantity(value) == expected


def test_coerce_quantity_uses_default_for_missing_values():
    assert coerce_quantity(None, default='4') == 4
    assert coerce_quantity(None, default='²') is None
