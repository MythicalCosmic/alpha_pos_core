from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections

from stock.models import Recipe, StockTransaction
from stock.services.order_service import OrderStockService
from stock.services.recipe_service import RecipeService

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"
    ),
]


def _race(operation):
    barrier = Barrier(2)

    def worker(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return operation(index)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(worker, index) for index in range(2)]
        return [future.result(timeout=20) for future in futures]


def test_concurrent_recipe_versions_have_distinct_numbers_and_codes(inventory_catalog):
    recipe_id = inventory_catalog.recipe.id
    results = _race(
        lambda index: RecipeService.create_new_version(
            recipe_id, output_quantity=20 + index
        )
    )
    assert [status for _, status in results] == [200, 200], results
    drafts = Recipe.objects.filter(parent_recipe_id=recipe_id)
    assert sorted(drafts.values_list("version", flat=True)) == [2, 3]
    assert drafts.values("code").distinct().count() == 2
    assert sorted(drafts.values_list("output_quantity", flat=True)) == [20, 21]


def test_competing_orders_cannot_reserve_the_same_available_stock(
    inventory_catalog, admin_user
):
    c = inventory_catalog
    c.level.quantity = 150
    c.level.save(update_fields=["quantity"])
    results = _race(
        lambda index: OrderStockService.reserve_for_order(
            2001 + index,
            [{"product_id": c.product.id}],
            c.location.id,
            admin_user.id,
        )
    )
    assert sorted(status for _, status in results) == [200, 400], results
    c.level.refresh_from_db()
    assert c.level.reserved_quantity == 100
    assert (
        StockTransaction.objects.filter(
            reference_id__in=[2001, 2002],
            movement_type="RESERVATION",
        ).count()
        == 1
    )
