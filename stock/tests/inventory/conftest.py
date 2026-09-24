"""Shared fixtures for the inventory audit regression tests."""
from types import SimpleNamespace

import pytest
from stock.models import (
    ProductStockLink, Recipe, RecipeIngredient, StockItem,
    StockLevel, StockLocation, StockSettings, StockUnit,
)


@pytest.fixture
def catalog(admin_user, product, settings):
    settings.BRANCH_ID = 'branch1'
    gram = StockUnit.objects.create(name='Audit gram', short_name='ag',
                                    unit_type='WEIGHT', is_base_unit=True)
    kilo = StockUnit.objects.create(name='Audit kilogram', short_name='akg',
                                    unit_type='WEIGHT', base_unit=gram,
                                    conversion_factor=1000)
    source = StockLocation.objects.create(name='Audit source', branch_id='branch1')
    dest = StockLocation.objects.create(name='Audit destination', branch_id='branch1')
    item = StockItem.objects.create(name='Audit flour', sku='AUDIT-FLOUR',
                                    base_unit=gram, avg_cost_price=2,
                                    branch_id='branch1')
    output = StockItem.objects.create(name='Audit dough', sku='AUDIT-DOUGH',
                                      base_unit=gram, branch_id='branch1')
    level = StockLevel.objects.create(stock_item=item, location=source,
                                      quantity=10000, branch_id='branch1')
    config = StockSettings.load()
    config.stock_enabled = True
    config.auto_deduct_on_sale = True
    config.allow_negative_stock = False
    config.track_batches = False
    config.default_location = source
    config.default_production_location = dest
    config.save()
    ProductStockLink.objects.create(product=product, link_type='DIRECT_ITEM',
                                    stock_item=item, unit=gram, quantity_per_sale=1)
    recipe = Recipe.objects.create(name='Audit dough recipe', code='AUDIT-DOUGH',
                                    output_item=output, output_unit=kilo,
                                    output_quantity=1, branch_id='branch1')
    ingredient = RecipeIngredient.objects.create(recipe=recipe, stock_item=item,
                                                  quantity=1, unit=kilo)
    return SimpleNamespace(gram=gram, kilo=kilo, source=source, dest=dest,
                           item=item, output=output, level=level, config=config,
                           product=product, recipe=recipe, ingredient=ingredient,
                           actor=admin_user)
