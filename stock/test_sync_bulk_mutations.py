import pytest
from django.utils import timezone


pytestmark = pytest.mark.django_db


def _mark_synced(instance):
    type(instance).objects.filter(pk=instance.pk).update(synced_at=timezone.now())
    instance.refresh_from_db()


def test_clearing_old_default_location_is_sync_visible():
    from stock.models import StockLocation
    from stock.repositories.location import StockLocationRepository

    old = StockLocation.objects.create(
        name='Old default', type='STORAGE', is_default=True,
    )
    _mark_synced(old)
    version = old.sync_version

    changed = StockLocationRepository.clear_default()

    old.refresh_from_db()
    assert changed == 1
    assert old.is_default is False
    assert old.sync_version == version + 1
    assert old.synced_at is None


def test_bulk_product_soft_delete_is_sync_visible(category):
    from base.models import Product
    from base.repositories.product import ProductRepository

    product = Product.objects.create(name='Retired', price='1', category=category)
    _mark_synced(product)
    version = product.sync_version

    changed = ProductRepository.bulk_soft_delete([product.id])

    product.refresh_from_db()
    assert changed == 1
    assert product.is_deleted is True
    assert product.sync_version == version + 1
    assert product.synced_at is None


def test_location_stock_value_uses_quantity_times_cost():
    from decimal import Decimal
    from stock.models import StockItem, StockLevel, StockLocation, StockUnit
    from stock.repositories.location import StockLocationRepository

    unit = StockUnit.objects.create(
        name='kilogram', short_name='kg-value', unit_type='WEIGHT',
    )
    location = StockLocation.objects.create(name='Valued store', type='STORAGE')
    stock_item = StockItem.objects.create(
        name='Valued flour', base_unit=unit, item_type='RAW',
        avg_cost_price=Decimal('50000'),
    )
    StockLevel.objects.create(
        stock_item=stock_item, location=location, quantity=Decimal('10'),
    )

    stats = StockLocationRepository.get_stock_stats(location)

    assert stats['total_qty'] == Decimal('10')
    assert stats['total_value'] == Decimal('500000')
