"""Regression tests for stock math/correctness bugs."""
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def base_unit(db):
    from stock.models import StockUnit
    return StockUnit.objects.create(name='kilogram', short_name='kg', unit_type='WEIGHT')


@pytest.fixture
def location(db):
    from stock.models import StockLocation
    return StockLocation.objects.create(name='Main Storage', type='STORAGE')


@pytest.fixture
def stock_item(db, base_unit):
    from stock.models import StockItem
    return StockItem.objects.create(
        name='Flour', base_unit=base_unit, item_type='RAW',
        cost_price=Decimal('10'), avg_cost_price=Decimal('10'),
        last_cost_price=Decimal('10'),
    )


@pytest.fixture
def stock_enabled(db):
    """StockSettings is a singleton that defaults stock_enabled=False, which
    short-circuits StockLevelService.adjust() to a no-op. Tests that exercise
    real stock math need this fixture so they're not silently skipped."""
    from stock.models import StockSettings
    settings = StockSettings.load()
    settings.stock_enabled = True
    settings.auto_deduct_on_sale = True
    settings.allow_negative_stock = False
    settings.save()
    return settings


class TestWeightedAverageCost:
    """Pre-fix: update_cost divided new_cost by total_qty+1 regardless of
    received quantity. Receiving 100kg @ 12 vs 100kg @ 10 prior should
    yield avg = 11, not (1000 + 12) / 101 = 10.02."""

    def test_moving_average_correct_for_equal_qty_receipt(
        self, stock_item, location,
    ):
        from stock.repositories import StockLevelRepository
        from stock.services.item_service import StockItemService

        # Seed: 100kg already on hand at avg cost 10
        level = StockLevelRepository.get_or_create_level(
            stock_item.id, location.id,
        )
        level.quantity = Decimal('200')  # post-receipt qty (caller adjusts first)
        level.save()

        result, status = StockItemService.update_cost(
            stock_item.id, new_cost=Decimal('12'),
            update_type='AVG', received_qty=Decimal('100'),
        )
        assert status == 200
        stock_item.refresh_from_db()
        # (100 * 10 + 100 * 12) / 200 = 11
        assert stock_item.avg_cost_price == Decimal('11.0000')

    def test_first_receipt_sets_cost(self, stock_item, location):
        from stock.repositories import StockLevelRepository
        from stock.services.item_service import StockItemService

        level = StockLevelRepository.get_or_create_level(
            stock_item.id, location.id,
        )
        level.quantity = Decimal('50')
        level.save()

        result, status = StockItemService.update_cost(
            stock_item.id, new_cost=Decimal('15'),
            update_type='AVG', received_qty=Decimal('50'),
        )
        assert status == 200
        stock_item.refresh_from_db()
        assert stock_item.avg_cost_price == Decimal('15.0000')


class TestStockCountVarianceDirection:
    """Pre-fix: COUNT_ADJUSTMENT was abs()'d and sign was inferred from a
    magic outgoing list. Negative variance (shrinkage) became a gain."""

    def test_negative_variance_decreases_stock(self, stock_item, location, stock_enabled, admin_user):
        from stock.repositories import StockLevelRepository
        from stock.services.level_service import StockLevelService

        level = StockLevelRepository.get_or_create_level(
            stock_item.id, location.id,
        )
        level.quantity = Decimal('100')
        level.save()

        # COUNT_ADJUSTMENT with negative quantity = shrinkage
        result, status = StockLevelService.adjust(
            stock_item_id=stock_item.id,
            location_id=location.id,
            quantity=Decimal('-5'),
            movement_type='COUNT_ADJUSTMENT',
            user_id=admin_user.id,
        )
        assert status == 200
        level.refresh_from_db()
        assert level.quantity == Decimal('95'), 'shrinkage must decrease stock'

    def test_positive_variance_increases_stock(self, stock_item, location, stock_enabled, admin_user):
        from stock.repositories import StockLevelRepository
        from stock.services.level_service import StockLevelService

        level = StockLevelRepository.get_or_create_level(
            stock_item.id, location.id,
        )
        level.quantity = Decimal('100')
        level.save()

        result, status = StockLevelService.adjust(
            stock_item_id=stock_item.id,
            location_id=location.id,
            quantity=Decimal('3'),
            movement_type='COUNT_ADJUSTMENT',
            user_id=admin_user.id,
        )
        assert status == 200
        level.refresh_from_db()
        assert level.quantity == Decimal('103')


class TestRecipeOverDeduction:
    """Pre-fix: recipe-linked sales deducted ingredients for the FULL recipe
    yield, not divided by recipe.output_quantity. Selling 1 cookie deducted
    ingredients for all 10 cookies in the recipe."""

    def test_recipe_link_divides_by_output_quantity(
        self, stock_item, base_unit, db,
    ):
        from base.models import Category, Product
        from stock.models import (
            Recipe, RecipeIngredient, ProductStockLink, StockItem,
        )
        from stock.services.product_link_service import ProductStockLinkService

        # Output item (the cookie) and ingredient item (flour, already created)
        output_item = StockItem.objects.create(
            name='Cookie', base_unit=base_unit, item_type='FINISHED',
        )

        recipe = Recipe.objects.create(
            name='Cookie Recipe', code='COOKIE-1',
            output_item=output_item, output_quantity=Decimal('10'),
            output_unit=base_unit,
        )
        # Recipe needs 1kg flour to make 10 cookies
        RecipeIngredient.objects.create(
            recipe=recipe, stock_item=stock_item, quantity=Decimal('1'),
            unit=base_unit,
        )

        category = Category.objects.create(name='Bakery')
        product = Product.objects.create(
            name='Cookie', price='2.00', category=category,
        )
        ProductStockLink.objects.create(
            product=product, link_type='RECIPE', recipe=recipe,
            quantity_per_sale=Decimal('1'),
        )

        # Selling 1 cookie should deduct 1/10 kg flour, not 1kg
        deductions = ProductStockLinkService.get_deduction_items(
            product.id, quantity=1,
        )
        assert len(deductions) == 1
        assert deductions[0]['stock_item_id'] == stock_item.id
        assert deductions[0]['quantity'] == Decimal('0.1'), (
            f"expected 0.1 (1kg / 10 cookies * 1 sold), got {deductions[0]['quantity']}"
        )


class TestDocumentNumberAllocation:
    """Pre-fix: generate_number read the max existing number and added 1 with
    no lock. Two concurrent sales computed the SAME transaction_number; the
    second insert violated the unique constraint and aborted the sale's stock
    deduction. Now backed by a select_for_update-locked SequenceCounter."""

    def test_adjust_issues_unique_sequential_numbers(
        self, stock_item, location, stock_enabled, admin_user,
    ):
        from stock.repositories import StockLevelRepository
        from stock.services.level_service import StockLevelService

        level = StockLevelRepository.get_or_create_level(stock_item.id, location.id)
        level.quantity = Decimal('100')
        level.save()

        numbers = []
        for _ in range(5):
            result, status = StockLevelService.adjust(
                stock_item_id=stock_item.id,
                location_id=location.id,
                quantity=Decimal('-1'),
                movement_type='SALE_OUT',
                user_id=admin_user.id,
            )
            assert status == 200, result
            numbers.append(result['data']['transaction_number'])

        assert len(set(numbers)) == 5, f'numbers must be unique, got {numbers}'
        # Sequential within the day's scope: TRX-YYYYMMDD-0001..0005
        suffixes = sorted(int(n.split('-')[-1]) for n in numbers)
        assert suffixes == [1, 2, 3, 4, 5], suffixes

    def test_counter_seeds_from_existing_max(
        self, stock_item, location, base_unit, admin_user,
    ):
        """A row created before the counter existed must not be re-issued:
        the counter seeds itself from the current max for the scope."""
        from django.utils import timezone
        from stock.models import StockTransaction
        from stock.services.base_service import generate_number

        date_part = timezone.now().strftime('%Y%m%d')
        StockTransaction.objects.create(
            transaction_number=f'TRX-{date_part}-0007',
            stock_item=stock_item, location=location, unit=base_unit,
            movement_type='ADJUSTMENT_PLUS',
            quantity=Decimal('1'), base_quantity=Decimal('1'),
            quantity_before=Decimal('0'), quantity_after=Decimal('1'),
            user=admin_user,
        )

        nxt = generate_number('TRX', StockTransaction, 'transaction_number')
        assert nxt == f'TRX-{date_part}-0008', (
            f'must continue past existing max 0007, got {nxt}'
        )


class TestReserveForOrderHonorsFailures:
    """Pre-fix: reserve_for_order discarded the (result, status) from
    StockLevelService.reserve and appended every item as 'reserved', so an
    insufficient-stock failure was reported as success and the order proceeded
    against stock that was never held (oversell)."""

    def _setup_recipe_product(self, stock_item, base_unit):
        from base.models import Category, Product
        from stock.models import Recipe, RecipeIngredient, ProductStockLink, StockItem

        output_item = StockItem.objects.create(
            name='Cookie', base_unit=base_unit, item_type='FINISHED',
        )
        recipe = Recipe.objects.create(
            name='Cookie Recipe', code='COOKIE-R', output_item=output_item,
            output_quantity=Decimal('10'), output_unit=base_unit,
        )
        RecipeIngredient.objects.create(
            recipe=recipe, stock_item=stock_item, quantity=Decimal('1'), unit=base_unit,
        )
        category = Category.objects.create(name='Bakery')
        product = Product.objects.create(name='Cookie', price='2.00', category=category)
        ProductStockLink.objects.create(
            product=product, link_type='RECIPE', recipe=recipe,
            quantity_per_sale=Decimal('1'),
        )
        return product

    def test_insufficient_stock_returns_error_and_does_not_reserve(
        self, stock_item, location, base_unit, admin_user,
    ):
        from stock.models import StockSettings
        from stock.repositories import StockLevelRepository
        from stock.services.order_service import OrderStockService

        settings = StockSettings.load()
        settings.stock_enabled = True
        settings.reserve_on_order_create = True
        settings.allow_negative_stock = False
        settings.save()

        product = self._setup_recipe_product(stock_item, base_unit)

        # Flour level exists but is empty → reservation must fail.
        level = StockLevelRepository.get_or_create_level(stock_item.id, location.id)
        level.quantity = Decimal('0')
        level.reserved_quantity = Decimal('0')
        level.save()

        result, status = OrderStockService.reserve_for_order(
            order_id=999,
            order_items=[{'product_id': product.id, 'quantity': 1}],
            location_id=location.id,
            user_id=admin_user.id,
        )

        assert status >= 400, f'insufficient stock must error, got {status}: {result}'
        assert result['success'] is False
        # Rolled back: nothing left reserved.
        level.refresh_from_db()
        assert level.reserved_quantity == Decimal('0'), 'must not hold any reservation'


class TestSupplierLedger:
    """Supplier debt ledger (P5): receiving creates debt, paying reduces it."""

    def _supplier(self):
        from stock.models import Supplier
        return Supplier.objects.create(name='ACME Foods')

    def test_record_purchase_increases_balance(self):
        from stock.services.supplier_ledger_service import SupplierLedgerService
        from stock.models import Supplier
        s = self._supplier()
        SupplierLedgerService.record_purchase(s.id, Decimal('50000'),
                                              reference_type='Test')
        s.refresh_from_db()
        assert s.current_balance == Decimal('50000.00')

    def test_pay_supplier_from_safe_reduces_balance_and_debits_treasury(self):
        from decimal import Decimal as D
        from stock.services.supplier_ledger_service import SupplierLedgerService
        from stock.models import Supplier
        from base.models import TreasuryAccount
        TreasuryAccount.objects.create(kind='SAFE', balance=D('100000'))
        s = self._supplier()
        SupplierLedgerService.record_purchase(s.id, D('50000'))
        result, status = SupplierLedgerService.pay_supplier(
            s.id, D('30000'), source_account='SAFE')
        assert status == 200, result
        s.refresh_from_db()
        assert s.current_balance == D('20000.00')  # 50k owed - 30k paid
        assert TreasuryAccount.objects.get(kind='SAFE').balance == D('70000.00')

    def test_pay_supplier_insufficient_safe_is_rejected(self):
        from decimal import Decimal as D
        from stock.services.supplier_ledger_service import SupplierLedgerService
        from base.models import TreasuryAccount
        TreasuryAccount.objects.create(kind='SAFE', balance=D('100'))
        s = self._supplier()
        SupplierLedgerService.record_purchase(s.id, D('50000'))
        result, status = SupplierLedgerService.pay_supplier(
            s.id, D('30000'), source_account='SAFE')
        assert status >= 400
        s.refresh_from_db()
        # Treasury rejected before the supplier ledger moved.
        assert s.current_balance == D('50000.00')

    def test_bank_payment_commission_debits_amount_plus_fee(self):
        from decimal import Decimal as D
        from stock.services.supplier_ledger_service import SupplierLedgerService
        from base.models import TreasuryAccount
        TreasuryAccount.objects.create(kind='BANK', balance=D('100000'))
        s = self._supplier()
        SupplierLedgerService.record_purchase(s.id, D('50000'))
        result, status = SupplierLedgerService.pay_supplier(
            s.id, D('30000'), source_account='BANK', commission=D('500'))
        assert status == 200, result
        # amount + fee left the bank.
        assert TreasuryAccount.objects.get(kind='BANK').balance == D('69500.00')
        s.refresh_from_db()
        assert s.current_balance == D('20000.00')  # debt reduced by amount only
