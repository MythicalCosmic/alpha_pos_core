"""Regression tests for discount bugs."""
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def percentage_type(db):
    from discounts.models import DiscountType
    return DiscountType.objects.create(
        name='Percentage', code='PCT', discount_method='PERCENTAGE',
    )


@pytest.fixture
def discount(db, percentage_type, admin_user):
    from discounts.models import Discount
    return Discount.objects.create(
        name='10% off', code='SAVE10', discount_type=percentage_type,
        value=Decimal('10'), is_active=True, applies_to='ENTIRE_ORDER',
        usage_limit=2, created_by=admin_user,
    )


class TestFilterActiveAndValid:
    """Pre-fix: DiscountRepository.filter_active_and_valid used
    Q(usage_count__lt=Q('usage_limit')) which is invalid — Q cannot wrap a
    field reference. The path raised on every call. Verify it now executes."""

    def test_filter_returns_active_discount(self, discount):
        from discounts.repositories import DiscountRepository
        results = list(DiscountRepository.filter_active_and_valid())
        assert discount in results

    def test_filter_excludes_at_limit(self, discount):
        from discounts.repositories import DiscountRepository
        from discounts.models import Discount
        Discount.objects.filter(pk=discount.pk).update(usage_count=2)
        results = list(DiscountRepository.filter_active_and_valid())
        assert discount not in results


class TestSerializerDoesNotLeakSecretWord:
    """Pre-fix: _serialize_discount returned secret_word — any admin could
    enumerate the codeword via the list endpoint."""

    def test_secret_word_not_in_serialized_output(self, discount):
        from discounts.models import Discount
        from discounts.services.discount_service import _serialize_discount

        Discount.objects.filter(pk=discount.pk).update(secret_word='hunter2')
        discount.refresh_from_db()

        data = _serialize_discount(discount)
        assert 'secret_word' not in data
        assert data.get('has_secret_word') is True
