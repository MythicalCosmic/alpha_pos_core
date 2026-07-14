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


class TestComponentModifierStockIntegrity:
    """Component modifiers must describe the stock actually consumed."""

    def _product(self, base_unit, stock_item, *, name='Burger'):
        from base.models import Category, Product
        from stock.models import ProductComponentStock, ProductStockLink, StockItem

        category, _ = Category.objects.get_or_create(name='Modifier Meals')
        product = Product.objects.create(
            name=name, price=Decimal('25000'), category=category,
        )
        link = ProductStockLink.objects.create(
            product=product, link_type='COMPONENT_BASED',
        )
        default = ProductComponentStock.objects.create(
            product_stock_link=link, component_name=f'{name} default',
            stock_item=stock_item, quantity=Decimal('2'), unit=base_unit,
            is_default=True, is_removable=True,
        )
        extra_item = StockItem.objects.create(
            name=f'{name} extra', base_unit=base_unit, item_type='RAW',
            cost_price=Decimal('10'), avg_cost_price=Decimal('10'),
            last_cost_price=Decimal('10'),
        )
        extra = ProductComponentStock.objects.create(
            product_stock_link=link, component_name=f'{name} extra',
            stock_item=extra_item, quantity=Decimal('1'), unit=base_unit,
            is_default=False, is_addable=True,
        )
        return product, default, extra, extra_item

    def test_remove_skips_default_and_duplicate_add_is_idempotent(
        self, base_unit, stock_item,
    ):
        from stock.services.product_link_service import ProductStockLinkService

        product, default, extra, extra_item = self._product(
            base_unit, stock_item,
        )
        modifiers = [
            {'component_id': default.id, 'action': 'REMOVE'},
            {'component_id': extra.id, 'action': 'ADD'},
            {'component_id': extra.id, 'action': 'ADD'},
        ]

        deductions = ProductStockLinkService.get_deduction_items(
            product.id, quantity=3, modifiers=modifiers,
        )

        assert deductions == [{
            'stock_item_id': extra_item.id,
            'quantity': Decimal('3'),
            'unit_id': base_unit.id,
            'component_id': extra.id,
            'modifier': 'ADD',
        }]

    def test_component_from_another_product_cannot_modify_this_product(
        self, base_unit, stock_item,
    ):
        from stock.services.product_link_service import ProductStockLinkService

        product, default, _extra, _extra_item = self._product(
            base_unit, stock_item, name='Burger A',
        )
        _other, other_default, _other_extra, _other_item = self._product(
            base_unit, stock_item, name='Burger B',
        )

        deductions = ProductStockLinkService.get_deduction_items(
            product.id, quantity=1,
            modifiers=[{'component_id': other_default.id, 'action': 'REMOVE'}],
        )

        assert len(deductions) == 1
        assert deductions[0]['component_id'] == default.id
        assert deductions[0]['quantity'] == Decimal('2')

    def test_deduct_and_cancel_reverse_only_components_actually_consumed(
        self, base_unit, stock_item, location, stock_enabled, admin_user,
    ):
        from base.models import Order
        from stock.models import StockTransaction
        from stock.repositories import StockLevelRepository
        from stock.services.order_service import OrderStockService

        product, default, extra, extra_item = self._product(
            base_unit, stock_item,
        )
        default_level = StockLevelRepository.get_or_create_level(
            stock_item.id, location.id,
        )
        extra_level = StockLevelRepository.get_or_create_level(
            extra_item.id, location.id,
        )
        default_level.quantity = Decimal('10')
        extra_level.quantity = Decimal('10')
        default_level.save()
        extra_level.save()
        order = Order.objects.create(
            user=admin_user, cashier=admin_user, status='PREPARING',
        )
        order_items = [{
            'product_id': product.id,
            'quantity': 2,
            'modifiers': [
                {'component_id': default.id, 'action': 'REMOVE'},
                {'component_id': extra.id, 'action': 'ADD'},
            ],
        }]

        result, status = OrderStockService.deduct_for_order(
            order.id, order_items, location.id, admin_user.id,
            order_status='PREPARING',
        )

        assert status == 200, result
        default_level.refresh_from_db()
        extra_level.refresh_from_db()
        assert default_level.quantity == Decimal('10')
        assert extra_level.quantity == Decimal('8')
        sold = StockTransaction.objects.filter(
            order=order, movement_type='SALE_OUT',
        )
        assert sold.count() == 1
        assert sold.get().stock_item_id == extra_item.id

        result, status = OrderStockService.reverse_deduction(
            order.id, admin_user.id,
        )
        assert status == 200, result
        default_level.refresh_from_db()
        extra_level.refresh_from_db()
        assert default_level.quantity == Decimal('10')
        assert extra_level.quantity == Decimal('10')


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


class TestOrderItemStockAdjustmentAtomicity:
    """A multi-component order edit must never leave partial inventory."""

    def test_later_component_failure_rolls_back_earlier_adjustment(
        self, monkeypatch, stock_item, location, stock_enabled, admin_user,
    ):
        from django.db.models import F
        from base.helpers.response import ServiceResponse
        from stock.repositories import (
            StockLevelRepository, StockTransactionRepository,
        )
        from stock.services.level_service import StockLevelService
        from stock.services.order_service import OrderStockService
        from stock.services.product_link_service import ProductStockLinkService

        level = StockLevelRepository.get_or_create_level(
            stock_item.id, location.id,
        )
        level.quantity = Decimal('10')
        level.save()

        monkeypatch.setattr(
            StockTransactionRepository,
            'exists_for_order',
            classmethod(lambda cls, order_id: True),
        )
        monkeypatch.setattr(
            ProductStockLinkService,
            'get_deduction_items',
            classmethod(lambda cls, product_id, quantity: [
                {'stock_item_id': stock_item.id, 'quantity': Decimal('1'),
                 'unit_id': None},
                {'stock_item_id': stock_item.id, 'quantity': Decimal('2'),
                 'unit_id': None},
            ]),
        )
        calls = {'count': 0}

        def staged_adjust(cls, **kwargs):
            calls['count'] += 1
            if calls['count'] == 1:
                type(level).objects.filter(pk=level.pk).update(
                    quantity=F('quantity') - Decimal('1'),
                )
                return ServiceResponse.success(data={'transaction_id': 1})
            return ServiceResponse.error('forced second component failure')

        monkeypatch.setattr(
            StockLevelService, 'adjust', classmethod(staged_adjust),
        )

        result, status = OrderStockService.adjust_for_item_change(
            order_id=1234,
            product_id=5678,
            quantity_delta=1,
            location_id=location.id,
            user_id=admin_user.id,
        )

        assert status == 400, result
        assert calls['count'] == 2
        level.refresh_from_db()
        assert level.quantity == Decimal('10')

    def test_status_handler_propagates_failure_and_rolls_back_prior_action(
        self, monkeypatch, stock_item, location, stock_enabled, admin_user,
    ):
        from django.db.models import F
        from base.helpers.response import ServiceResponse
        from stock.repositories import StockLevelRepository
        from stock.services.order_service import (
            OrderStatusHandler, OrderStockService,
        )

        stock_enabled.reserve_on_order_create = True
        stock_enabled.deduct_on_order_status = 'PREPARING'
        stock_enabled.save()
        level = StockLevelRepository.get_or_create_level(
            stock_item.id, location.id,
        )
        level.quantity = Decimal('10')
        level.save()

        def reserve(cls, *args, **kwargs):
            type(level).objects.filter(pk=level.pk).update(
                quantity=F('quantity') - Decimal('1'),
            )
            return ServiceResponse.success(data={'reserved': True})

        monkeypatch.setattr(
            OrderStockService, 'reserve_for_order', classmethod(reserve),
        )
        monkeypatch.setattr(
            OrderStockService, 'release_reservation',
            classmethod(lambda cls, *args, **kwargs:
                        ServiceResponse.success(data={'released': True})),
        )
        monkeypatch.setattr(
            OrderStockService, 'deduct_for_order',
            classmethod(lambda cls, *args, **kwargs:
                        ServiceResponse.error('forced deduction failure')),
        )

        result, status = OrderStatusHandler.on_status_change(
            order_id=1234,
            old_status=None,
            new_status='PREPARING',
            order_items=[{'product_id': 1, 'quantity': 1}],
            location_id=location.id,
            user_id=admin_user.id,
        )

        assert status == 400, result
        level.refresh_from_db()
        assert level.quantity == Decimal('10')


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

    def test_purchase_order_payment_is_locked_and_audited(
        self, location, admin_user,
    ):
        from django.utils import timezone
        from stock.models import PurchaseOrder, SupplierTransaction
        from stock.services.purchase_service import PurchaseOrderService

        supplier = self._supplier()
        supplier.current_balance = Decimal('100000.00')
        supplier.save(update_fields=['current_balance'])
        po = PurchaseOrder.objects.create(
            order_number='PO-PAYMENT-LEDGER-1',
            supplier=supplier,
            delivery_location=location,
            order_date=timezone.localdate(),
            created_by=admin_user,
            total=Decimal('75000.00'),
        )

        result, status = PurchaseOrderService.record_payment(
            po.id, Decimal('25000.00'), notes='First instalment',
        )

        assert status == 200, result
        po.refresh_from_db()
        supplier.refresh_from_db()
        assert po.amount_paid == Decimal('25000.00')
        assert po.payment_status == PurchaseOrder.PaymentStatus.PARTIAL
        assert supplier.current_balance == Decimal('75000.00')

        payment = SupplierTransaction.objects.get(
            supplier=supplier,
            type=SupplierTransaction.Type.PAYMENT,
            reference_type='PurchaseOrder',
            reference_id=po.id,
        )
        assert payment.amount == Decimal('25000.00')
        assert payment.balance_before == Decimal('100000.00')
        assert payment.balance_after == Decimal('75000.00')
        assert payment.source_account == ''
        assert payment.note == 'First instalment'

    def test_payments_on_different_pos_do_not_overwrite_supplier_balance(
        self, location, admin_user,
    ):
        from django.utils import timezone
        from stock.models import PurchaseOrder, SupplierTransaction
        from stock.services.purchase_service import PurchaseOrderService

        supplier = self._supplier()
        supplier.current_balance = Decimal('100000.00')
        supplier.save(update_fields=['current_balance'])
        pos = [
            PurchaseOrder.objects.create(
                order_number=f'PO-PAYMENT-LEDGER-{index}',
                supplier=supplier,
                delivery_location=location,
                order_date=timezone.localdate(),
                created_by=admin_user,
                total=Decimal('50000.00'),
            )
            for index in (2, 3)
        ]

        for po in pos:
            result, status = PurchaseOrderService.record_payment(
                po.id, Decimal('20000.00'),
            )
            assert status == 200, result

        supplier.refresh_from_db()
        assert supplier.current_balance == Decimal('60000.00')
        payments = SupplierTransaction.objects.filter(
            supplier=supplier,
            type=SupplierTransaction.Type.PAYMENT,
            reference_type='PurchaseOrder',
        ).order_by('created_at')
        assert list(payments.values_list('balance_before', 'balance_after')) == [
            (Decimal('100000.00'), Decimal('80000.00')),
            (Decimal('80000.00'), Decimal('60000.00')),
        ]


class TestReceivingAtomicity:
    """A failed completion must not commit an inventory half-receipt."""

    def test_cost_failure_rolls_back_level_po_line_and_receiving(
        self, monkeypatch, stock_item, base_unit, location, stock_enabled,
        admin_user,
    ):
        from django.utils import timezone
        from stock.models import (
            PurchaseOrder, PurchaseOrderItem, PurchaseReceiving,
            PurchaseReceivingItem, StockLevel, StockTransaction, Supplier,
        )
        from stock.services.item_service import StockItemService
        from stock.services.purchase_service import PurchaseReceivingService

        supplier = Supplier.objects.create(name='Rollback Supplier')
        po = PurchaseOrder.objects.create(
            order_number='PO-ROLLBACK-1',
            supplier=supplier,
            delivery_location=location,
            order_date=timezone.localdate(),
            created_by=admin_user,
            total=Decimal('50.00'),
        )
        po_item = PurchaseOrderItem.objects.create(
            purchase_order=po,
            stock_item=stock_item,
            quantity_ordered=Decimal('5'),
            unit=base_unit,
            unit_price=Decimal('10'),
            total_price=Decimal('50'),
        )
        receiving = PurchaseReceiving.objects.create(
            receiving_number='RCV-ROLLBACK-1',
            purchase_order=po,
            location=location,
            received_date=timezone.localdate(),
            received_by=admin_user,
        )
        PurchaseReceivingItem.objects.create(
            receiving=receiving,
            po_item=po_item,
            stock_item=stock_item,
            quantity_received=Decimal('5'),
            unit=base_unit,
            unit_cost=Decimal('10'),
        )

        monkeypatch.setattr(
            StockItemService,
            'update_cost',
            lambda *args, **kwargs: ({'success': False, 'message': 'forced cost failure'}, 400),
        )

        result, status = PurchaseReceivingService.complete(receiving.id)

        assert status == 400, result
        receiving.refresh_from_db()
        po_item.refresh_from_db()
        supplier.refresh_from_db()
        assert receiving.status == PurchaseReceiving.Status.DRAFT
        assert po_item.quantity_received == Decimal('0')
        assert supplier.current_balance == Decimal('0')
        assert not StockLevel.objects.filter(
            stock_item=stock_item, location=location,
        ).exists()
        assert not StockTransaction.objects.filter(
            reference_type='PurchaseReceiving', reference_id=receiving.id,
        ).exists()

    def test_sequential_receivings_accumulate_on_locked_po_line(
        self, stock_item, base_unit, location, stock_enabled, admin_user,
    ):
        from django.utils import timezone
        from stock.models import (
            PurchaseOrder, PurchaseOrderItem, PurchaseReceiving,
            PurchaseReceivingItem, Supplier,
        )
        from stock.services.purchase_service import PurchaseReceivingService

        supplier = Supplier.objects.create(name='Concurrent Receipt Supplier')
        po = PurchaseOrder.objects.create(
            order_number='PO-ACCUMULATE-1',
            supplier=supplier,
            delivery_location=location,
            order_date=timezone.localdate(),
            created_by=admin_user,
            total=Decimal('50.00'),
        )
        po_item = PurchaseOrderItem.objects.create(
            purchase_order=po,
            stock_item=stock_item,
            quantity_ordered=Decimal('5'),
            unit=base_unit,
            unit_price=Decimal('10'),
            total_price=Decimal('50'),
        )
        start_version = po_item.sync_version

        for index, quantity in enumerate((Decimal('2'), Decimal('3')), start=1):
            receiving = PurchaseReceiving.objects.create(
                receiving_number=f'RCV-ACCUMULATE-{index}',
                purchase_order=po,
                location=location,
                received_date=timezone.localdate(),
                received_by=admin_user,
            )
            PurchaseReceivingItem.objects.create(
                receiving=receiving,
                po_item=po_item,
                stock_item=stock_item,
                quantity_received=quantity,
                unit=base_unit,
                unit_cost=Decimal('10'),
            )

            result, status = PurchaseReceivingService.complete(receiving.id)
            assert status == 200, result

        po_item.refresh_from_db()
        assert po_item.quantity_received == Decimal('5')
        assert po_item.sync_version == start_version + 2
