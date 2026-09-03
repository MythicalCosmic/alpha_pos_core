from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone

from base.models import TreasuryAccount, TreasuryTransaction, User
from base.services.treasury_service import TreasuryService, _apply, _lock_accounts
from stock.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceiving,
    StockBatch,
    StockItem,
    StockItemUnit,
    StockLevel,
    StockLocation,
    StockSettings,
    StockUnit,
    Supplier,
    SupplierPayment,
    SupplierTransaction,
)
from stock.services.inventory_control_service import _supplier_totals
from stock.services.purchase_service import PurchaseReceivingService
from stock.services.supplier_ledger_service import (
    SupplierLedgerService,
    SupplierPaymentService,
)


pytestmark = pytest.mark.django_db


def _user(email, branch='branch1'):
    return User.objects.create(
        first_name=email.split('@')[0],
        last_name='Tester',
        email=email,
        password='!',
        role=User.RoleChoices.ADMIN,
        status=User.UserStatus.ACTIVE,
        permissions=['*'],
        branch_id=branch,
    )


def _catalog(branch='branch1', *, alternate=False, tracked=False):
    base_unit = StockUnit.objects.create(
        name='Kilogram', short_name='kg', unit_type='WEIGHT',
        is_base_unit=True,
    )
    purchase_unit = base_unit
    if alternate:
        purchase_unit = StockUnit.objects.create(
            name='Bag', short_name='bag', unit_type='WEIGHT',
            base_unit=base_unit, conversion_factor='25',
        )
    location = StockLocation.objects.create(
        name='Warehouse', type='WAREHOUSE', branch_id=branch,
    )
    item = StockItem.objects.create(
        name='Flour', sku=f'FLOUR-{uuid4().hex[:8]}', base_unit=base_unit,
        item_type=StockItem.ItemType.RAW, track_batches=tracked,
        branch_id=branch,
    )
    if alternate:
        StockItemUnit.objects.create(
            stock_item=item,
            unit=purchase_unit,
            conversion_to_base='25',
        )
    return base_unit, purchase_unit, location, item


def _purchase_order(actor, supplier, location, item, unit, *, total='1200', quantity='1'):
    po = PurchaseOrder.objects.create(
        order_number=f'PO-{uuid4().hex[:8]}',
        supplier=supplier,
        delivery_location=location,
        status=PurchaseOrder.Status.CONFIRMED,
        order_date=timezone.localdate(),
        payment_due_date=timezone.now(),
        total=total,
        created_by=actor,
        branch_id=supplier.branch_id,
    )
    line = PurchaseOrderItem.objects.create(
        purchase_order=po,
        stock_item=item,
        quantity_ordered=quantity,
        unit=unit,
        unit_price=Decimal(total) / Decimal(quantity),
        total_price=total,
        branch_id=supplier.branch_id,
    )
    return po, line


def test_receiving_converts_to_base_units_and_reverses_same_cost_basis():
    receiver = _user('receiver@test.local')
    reviewer = _user('reviewer@test.local')
    base_unit, bag, location, item = _catalog(
        alternate=True, tracked=True,
    )
    supplier = Supplier.objects.create(name='Mill', branch_id='branch1')
    po, line = _purchase_order(
        receiver, supplier, location, item, bag,
        total='200000', quantity='2',
    )
    settings = StockSettings.load()
    settings.stock_enabled = True
    settings.save(update_fields=['stock_enabled', 'updated_at'])
    created, status = PurchaseReceivingService.create(
        po.id, receiver.id, location.id,
    )
    assert status == 200, created
    receiving_id = created['data']['id']
    added, status = PurchaseReceivingService.add_item(
        receiving_id,
        line.id,
        '2',
        unit_cost='100000',
        batch_number='MILL-2026-01',
    )
    assert status == 200, added
    completed, status = PurchaseReceivingService.complete(
        receiving_id,
        actor=receiver,
        action_id=uuid4(),
        idempotency_key='receive-one',
    )
    assert status == 200, completed
    received_item = PurchaseReceiving.objects.get(pk=receiving_id).items.get()
    assert received_item.conversion_to_base_snapshot == Decimal('25')
    assert received_item.base_quantity == Decimal('50')
    assert received_item.base_unit_id == base_unit.id
    assert received_item.base_unit_cost == Decimal('4000')
    assert StockLevel.objects.get(stock_item=item).quantity == Decimal('50')
    batch = StockBatch.objects.get(stock_item=item)
    assert batch.current_quantity == Decimal('50')
    assert batch.unit_cost == Decimal('4000')
    item.refresh_from_db()
    supplier.refresh_from_db()
    assert item.avg_cost_price == Decimal('4000')
    assert supplier.current_balance == Decimal('200000')
    assert completed['data']['supplier_id'] == supplier.id

    requested, status = PurchaseReceivingService.request_correction(
        receiving_id, receiver, 'Supplier recalled the batch',
    )
    assert status == 201, requested
    reversed_result, status = PurchaseReceivingService.review_correction(
        requested['data']['correction_id'],
        reviewer,
        True,
        'Approved supplier return',
    )
    assert status == 200, reversed_result
    item.refresh_from_db()
    supplier.refresh_from_db()
    batch.refresh_from_db()
    assert StockLevel.objects.get(stock_item=item).quantity == Decimal('0')
    assert batch.current_quantity == Decimal('0')
    assert item.avg_cost_price == Decimal('0')
    assert supplier.current_balance == Decimal('0')


def test_supplier_payment_cross_links_ledgers_allocates_and_reverses():
    actor = _user('supplier-pay@test.local')
    _base, unit, location, item = _catalog()
    supplier = Supplier.objects.create(name='Supplier', branch_id='branch1')
    po, _line = _purchase_order(
        actor, supplier, location, item, unit, total='1200',
    )
    receiving = PurchaseReceiving.objects.create(
        receiving_number=f'RCV-{uuid4().hex[:8]}',
        purchase_order=po,
        location=location,
        received_date=timezone.localdate(),
        received_by=actor,
        status=PurchaseReceiving.Status.COMPLETED,
        completed_at=timezone.now(),
        received_value_uzs='1200',
        branch_id='branch1',
    )
    purchase_row = SupplierLedgerService.record_purchase(
        supplier.id,
        Decimal('1200'),
        reference_type='PurchaseReceiving',
        reference_id=receiving.id,
        performed_by=actor,
    )
    receiving.supplier_transaction = purchase_row
    receiving.save(update_fields=['supplier_transaction'])
    bank = _lock_accounts(['BANK'], 'branch1')['BANK']
    _apply(
        bank, Decimal('5000'), TreasuryTransaction.Type.ADJUSTMENT,
        branch_id='branch1', performed_by=actor,
    )
    fee_error, status = SupplierPaymentService.pay(
        supplier.id,
        '1200',
        'SAFE',
        fee_uzs='10',
        allocation_mode='EXPLICIT',
        allocations=[{'purchase_order_id': po.id, 'amount_uzs': 1200}],
        actor=actor,
    )
    assert status == 422
    assert fee_error['code'] == 'FEE_BANK_ONLY'
    action_id = uuid4()
    paid, status = SupplierPaymentService.pay(
        supplier.id,
        '1200',
        'BANK',
        fee_uzs='10',
        allocation_mode='EXPLICIT',
        allocations=[{'purchase_order_id': po.id, 'amount_uzs': 1200}],
        actor=actor,
        action_id=action_id,
        idempotency_key='supplier-one',
    )
    assert status == 201, paid
    assert paid['data']['total_debited_uzs'] == 1210
    assert paid['data']['supplier_balance_after_uzs'] == 0
    assert paid['data']['source_balance_after_uzs'] == 3790
    assert paid['data']['allocations'][0]['remaining_uzs'] == 0
    replay, status = SupplierPaymentService.pay(
        supplier.id,
        '1200',
        'BANK',
        fee_uzs='10',
        allocation_mode='EXPLICIT',
        allocations=[{'purchase_order_id': po.id, 'amount_uzs': 1200}],
        actor=actor,
        action_id=action_id,
        idempotency_key='supplier-one',
    )
    assert status == 201
    assert replay['data']['payment_id'] == paid['data']['payment_id']
    assert SupplierPayment.objects.count() == 1

    reversed_result, status = SupplierPaymentService.reverse(
        paid['data']['payment_id'],
        actor=actor,
        reason='Transfer recalled',
        action_id=uuid4(),
        idempotency_key='supplier-reverse-one',
    )
    assert status == 200, reversed_result
    supplier.refresh_from_db()
    po.refresh_from_db()
    assert supplier.current_balance == Decimal('1200')
    assert po.amount_paid == Decimal('0')
    assert TreasuryAccount.objects.get(kind='BANK').balance == Decimal('5000')
    assert SupplierTransaction.objects.filter(supplier=supplier).count() == 3
    payable, credit, issues = _supplier_totals('branch1')
    assert (payable, credit, issues) == (Decimal('1200'), Decimal('0'), [])


def test_history_totals_cover_filtered_rows_before_pagination():
    actor = _user('history@test.local')
    supplier = Supplier.objects.create(name='History Supplier', branch_id='branch1')
    SupplierLedgerService.record_purchase(supplier.id, Decimal('100'), performed_by=actor)
    SupplierLedgerService.record_purchase(supplier.id, Decimal('200'), performed_by=actor)

    supplier_result, status = SupplierLedgerService.history(
        supplier.id,
        actor=actor,
        page=1,
        per_page=1,
        transaction_type='PURCHASE',
    )
    assert status == 200
    assert len(supplier_result['data']['transactions']) == 1
    assert supplier_result['data']['totals'] == {
        'principal_uzs': 300,
        'payable_increase_uzs': 300,
        'payable_decrease_uzs': 0,
        'row_count': 2,
    }

    account = _lock_accounts(['SAFE'], 'branch1')['SAFE']
    _apply(
        account, Decimal('100'), TreasuryTransaction.Type.ADJUSTMENT,
        description='first', branch_id='branch1', performed_by=actor,
    )
    _apply(
        account, Decimal('200'), TreasuryTransaction.Type.ADJUSTMENT,
        description='second', branch_id='branch1', performed_by=actor,
    )
    treasury_result, status = TreasuryService.history(
        account_kind='SAFE',
        txn_type='ADJUSTMENT',
        actor=actor,
        page=1,
        per_page=1,
    )
    assert status == 200
    assert len(treasury_result['data']['transactions']) == 1
    assert treasury_result['data']['totals'] == {
        'total_inflow_uzs': 300,
        'total_outflow_uzs': 0,
        'total_fee_uzs': 0,
        'row_count': 2,
    }


def test_treasury_history_counts_transfer_fee_once_with_and_without_account_filter():
    actor = _user('transfer-fee-history@test.local')
    accounts = _lock_accounts(['SAFE', 'BANK'], 'branch1')
    safe = accounts['SAFE']
    _apply(
        safe, Decimal('1000'), TreasuryTransaction.Type.ADJUSTMENT,
        branch_id='branch1', performed_by=actor,
    )
    result, status = TreasuryService.transfer(
        'SAFE', 'BANK', '400', fee='25', performed_by=actor,
        branch_id='branch1', command_id=uuid4(),
        idempotency_key='transfer-fee-history',
    )
    assert status == 200, result

    unfiltered, status = TreasuryService.history(actor=actor, per_page=100)
    assert status == 200
    assert unfiltered['data']['totals']['total_fee_uzs'] == 25
    assert unfiltered['data']['totals']['row_count'] == 3

    safe_history, status = TreasuryService.history(
        actor=actor, account_kind='SAFE', per_page=100,
    )
    assert status == 200
    assert safe_history['data']['totals']['total_fee_uzs'] == 25
    bank_history, status = TreasuryService.history(
        actor=actor, account_kind='BANK', per_page=100,
    )
    assert status == 200
    assert bank_history['data']['totals']['total_fee_uzs'] == 25

    incoming, status = TreasuryService.history(
        actor=actor, txn_type=TreasuryTransaction.Type.TRANSFER_IN,
    )
    assert status == 200
    assert incoming['data']['totals']['total_fee_uzs'] == 25
